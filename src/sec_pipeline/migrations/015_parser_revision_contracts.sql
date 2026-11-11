-- New receipts require the heading-independent primary-statement gate, broader
-- appendix/signature boundaries, and direct numeric-tag adjacency checks.
-- Existing sealed rows and historical migration checksums remain unchanged.

CREATE OR REPLACE FUNCTION sec.validate_source_processing_set() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ds sec.source_document_sets%ROWTYPE; fact_count bigint; document_count bigint; table_count bigint;
        report_count bigint; chunk_count bigint; cell_count bigint; accepted_count bigint; source_only_count bigint;
        review_count bigint; issue_count bigint; target_table uuid;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO ds FROM sec.source_document_sets WHERE id=NEW.document_set_id AND filing_id=NEW.filing_id;
    IF ds.id IS NULL OR NOT (NEW.source_checks ?& ARRAY['document_set_complete','union_correspondence','numeric_values_compared',
        'source_cells_complete','source_bindings_exact','reports_complete','chunks_complete'])
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.source_checks) c WHERE btrim(c.key)='' OR c.value IS DISTINCT FROM 'true'::jsonb)
        OR NEW.semantic_checks='{}' OR NOT(NEW.semantic_checks ? 'period_header_consistency')
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.semantic_checks) c WHERE btrim(c.key)='' OR c.value NOT IN ('true'::jsonb,'false'::jsonb))
        OR NEW.coverage-ARRAY['facts','tables','reports','chunks']<>'{}'::jsonb
        OR NOT(NEW.coverage ?& ARRAY['facts','tables','reports','chunks'])
        OR EXISTS(SELECT 1 FROM jsonb_each_text(NEW.coverage) c WHERE c.value NOT IN ('partial','complete')
            AND NOT(c.key='reports' AND c.value='unknown')) THEN
        RAISE EXCEPTION 'V4 sets require complete explicit source checks and separate semantic assessment' USING ERRCODE='23514';
    END IF;
    IF NEW.versions->>'tables' IN ('tables-v5','tables-v6') AND (
        NEW.semantic_checks->'primary_section_boundaries' IS DISTINCT FROM 'true'::jsonb
        OR NEW.source_checks->'numeric_tag_boundaries_verified' IS DISTINCT FROM 'true'::jsonb
        OR NEW.configuration->'versions' IS DISTINCT FROM NEW.versions
        OR NEW.configuration->>'extraction_profile' IS DISTINCT FROM
           CASE NEW.versions->>'tables' WHEN 'tables-v5' THEN 'corrected-layout-v5' ELSE 'reviewed-layout-v6' END
        OR NEW.configuration->>'section_validation_policy' IS DISTINCT FROM
           CASE NEW.versions->>'tables' WHEN 'tables-v5' THEN 'original-heading-v1' ELSE 'source-boundaries-v2' END
        OR NEW.configuration->'parser_revisions' IS DISTINCT FROM
           CASE NEW.versions->>'tables' WHEN 'tables-v5' THEN
             '{"facts":"numeric-tag-boundaries-v3","reports":"corrected-visible-layout-boundaries-v4","tables":"registrant-numeric-cell-validation-v2"}'::jsonb
           ELSE
             '{"facts":"numeric-tag-boundaries-v3","reports":"reviewed-original-source-boundaries-v4","tables":"registrant-numeric-cell-validation-v2","chunks":"token-boundary-overlap-v2"}'::jsonb END) THEN
        RAISE EXCEPTION 'Reviewed sets require original source boundaries and current versioned receipts' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO fact_count FROM sec.financial_facts WHERE source_document_id=ds.xml_document_id AND extraction_version=NEW.versions->>'facts';
    IF fact_count=0 OR EXISTS((SELECT id FROM sec.financial_facts WHERE source_document_id=ds.xml_document_id AND extraction_version=NEW.versions->>'facts'
        EXCEPT SELECT fact_id FROM sec.fact_source_occurrences WHERE document_set_id=ds.id)
        UNION ALL (SELECT fact_id FROM sec.fact_source_occurrences WHERE document_set_id=ds.id
        EXCEPT SELECT id FROM sec.financial_facts WHERE source_document_id=ds.xml_document_id AND extraction_version=NEW.versions->>'facts')) THEN
        RAISE EXCEPTION 'V4 union correspondence must cover every selected XML occurrence exactly' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO document_count FROM sec.source_document_set_members WHERE document_set_id=ds.id;
    SELECT count(*) INTO table_count FROM sec.source_processing_tables WHERE processing_set_id=NEW.id;
    SELECT count(*) INTO report_count FROM sec.source_processing_reports WHERE processing_set_id=NEW.id;
    IF table_count=0 OR report_count<>document_count
        OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.financial_tables t ON t.id=member.table_id
            WHERE member.processing_set_id=NEW.id AND (t.extraction_version<>NEW.versions->>'tables' OR t.relational_cells_version IS DISTINCT FROM 3
             OR t.structure->>'schema_version' IS DISTINCT FROM '3' OR t.filing_id<>NEW.filing_id
             OR t.structure#>>'{fact_scope,xml_document_id}' IS DISTINCT FROM ds.xml_document_id::text
             OR t.structure#>>'{fact_scope,facts_version}' IS DISTINCT FROM NEW.versions->>'facts'
             OR NOT EXISTS(SELECT 1 FROM sec.source_document_set_members m WHERE m.document_set_id=ds.id AND m.source_document_id=t.source_document_id)))
        OR EXISTS((SELECT t.id FROM sec.financial_tables t JOIN sec.source_document_set_members m ON m.source_document_id=t.source_document_id
                   WHERE m.document_set_id=ds.id AND t.extraction_version=NEW.versions->>'tables'
                   EXCEPT SELECT table_id FROM sec.source_processing_tables WHERE processing_set_id=NEW.id)) THEN
        RAISE EXCEPTION 'V4 table selection must retain every schema3 member-source table' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM sec.source_processing_reports member JOIN sec.reports r ON r.id=member.report_id
        WHERE member.processing_set_id=NEW.id AND (r.filing_id<>NEW.filing_id OR r.extraction_version<>NEW.versions->>'reports'
          OR member.chunking_version<>NEW.versions->>'chunks'
          OR NOT EXISTS(SELECT 1 FROM sec.source_document_set_members m WHERE m.document_set_id=ds.id AND m.source_document_id=r.source_document_id)
          OR NOT EXISTS(SELECT 1 FROM sec.chunks c WHERE c.report_id=r.id AND c.chunking_version=member.chunking_version)))
        OR (SELECT count(DISTINCT r.source_document_id) FROM sec.source_processing_reports member JOIN sec.reports r ON r.id=member.report_id
            WHERE member.processing_set_id=NEW.id)<>document_count THEN
        RAISE EXCEPTION 'V4 reports/chunks must cover each original HTML document separately' USING ERRCODE='23514';
    END IF;
    FOR target_table IN SELECT table_id FROM sec.source_processing_tables WHERE processing_set_id=NEW.id LOOP
        PERFORM sec.validate_table_cell_consistency(target_table);
    END LOOP;
    IF EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.cell_fact_links link ON link.table_id=member.table_id
        JOIN sec.financial_facts fact ON fact.id=link.fact_id WHERE member.processing_set_id=NEW.id
        AND (fact.source_document_id<>ds.xml_document_id OR fact.extraction_version<>NEW.versions->>'facts'))
        OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.financial_tables t ON t.id=member.table_id
            JOIN sec.cell_fact_links link ON link.table_id=t.id WHERE member.processing_set_id=NEW.id
            AND NOT EXISTS(SELECT 1 FROM sec.fact_source_occurrences occurrence
                WHERE occurrence.document_set_id=ds.id AND occurrence.fact_id=link.fact_id
                  AND occurrence.html_document_id=t.source_document_id
                  AND occurrence.evidence->>'inline_xpath'=link.evidence->>'inline_xpath'))
        OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.cell_source_fact_bindings binding ON binding.table_id=member.table_id
            WHERE member.processing_set_id=NEW.id AND binding.document_set_id<>ds.id)
        OR EXISTS((SELECT issue.id FROM sec.source_quality_issues issue JOIN sec.cell_source_fact_bindings binding ON binding.id=issue.binding_id
                   JOIN sec.source_processing_tables member ON member.table_id=binding.table_id WHERE member.processing_set_id=NEW.id
                   EXCEPT SELECT issue_id FROM sec.source_processing_issues WHERE processing_set_id=NEW.id)
                  UNION ALL
                  (SELECT issue_id FROM sec.source_processing_issues WHERE processing_set_id=NEW.id EXCEPT
                   SELECT issue.id FROM sec.source_quality_issues issue JOIN sec.cell_source_fact_bindings binding ON binding.id=issue.binding_id
                   JOIN sec.source_processing_tables member ON member.table_id=binding.table_id WHERE member.processing_set_id=NEW.id AND issue.document_set_id=ds.id)) THEN
        RAISE EXCEPTION 'V4 selected fact links and quality issues must match their exact document-set scope' USING ERRCODE='23514';
    END IF;
    SELECT count(*) FILTER(WHERE c.kind='data'), count(*) FILTER(WHERE c.kind='data' AND c.resolution_status='resolved'),
        count(*) FILTER(WHERE c.resolution_status='source_only'), count(*) FILTER(WHERE c.resolution_status='review_required')
    INTO cell_count,accepted_count,source_only_count,review_count
    FROM sec.source_processing_tables member JOIN sec.table_cells c ON c.table_id=member.table_id WHERE member.processing_set_id=NEW.id;
    SELECT count(*) INTO issue_count FROM sec.source_processing_issues WHERE processing_set_id=NEW.id;
    IF cell_count=0 OR cell_count<>accepted_count+source_only_count+review_count OR (review_count=0) IS DISTINCT FROM (issue_count=0)
        OR NEW.semantic_checks->'period_header_consistency' IS DISTINCT FROM to_jsonb(issue_count=0)
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.semantic_checks) c WHERE c.value='false'::jsonb AND c.key<>'period_header_consistency') THEN
        RAISE EXCEPTION 'Every V4 financial cell and semantic exception must be explicitly accounted for' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO chunk_count FROM sec.source_processing_reports member JOIN sec.chunks c
        ON c.report_id=member.report_id AND c.chunking_version=member.chunking_version WHERE member.processing_set_id=NEW.id;
    NEW.validation_status:=CASE WHEN issue_count=0 THEN 'passed' ELSE 'review_required' END;
    NEW.counts:=jsonb_build_object('inline_documents',document_count,'facts',fact_count,'tables',table_count,'reports',report_count,'chunks',chunk_count,
        'financial_cells',cell_count,'accepted_cells',accepted_count,'source_only_cells',source_only_count,
        'review_required_cells',review_count,'quality_issues',issue_count,'unaccounted_cells',0);
    RETURN NEW;
END; $$;

-- Keep current selections and proven rollback history; reject fresh publication
-- of receipts that predate the current independent source checks.
CREATE OR REPLACE FUNCTION sec.guard_source_publication() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target sec.source_processing_sets%ROWTYPE; prior_family text; prior_id uuid; target_filing uuid; rollback_event bigint; reverting sec.source_publication_history%ROWTYPE;
BEGIN
 target_filing := CASE WHEN TG_OP='DELETE' THEN OLD.filing_id ELSE NEW.filing_id END;
 PERFORM sec.lock_processing_filing(target_filing);
 IF TG_OP='UPDATE' AND NEW.filing_id IS DISTINCT FROM OLD.filing_id THEN
   RAISE EXCEPTION 'Publication cannot move to another filing' USING ERRCODE='23514';
 END IF;
 IF TG_OP<>'DELETE' THEN
   SELECT * INTO target FROM sec.source_processing_sets
    WHERE id=NEW.processing_set_id AND filing_id=NEW.filing_id;
   IF target.id IS NULL OR target.validation_status<>'passed' OR target.source_validation_status<>'passed'
      OR target.versions->>'tables' NOT IN ('tables-v5','tables-v6')
      OR target.semantic_checks->'primary_section_boundaries' IS DISTINCT FROM 'true'::jsonb
      OR target.semantic_checks->'period_header_consistency' IS DISTINCT FROM 'true'::jsonb
      OR target.counts->>'quality_issues' IS DISTINCT FROM '0'
      OR target.counts->>'unaccounted_cells' IS DISTINCT FROM '0' THEN
     RAISE EXCEPTION 'Publication requires a passed corrected set without unresolved review issues' USING ERRCODE='23514';
   END IF;
   IF TG_OP='UPDATE' AND NEW.processing_set_id=OLD.processing_set_id THEN RETURN OLD; END IF;
   NEW.published_at := clock_timestamp();
 END IF;
 -- An upsert also fires the INSERT trigger before its UPDATE trigger. Only
 -- the UPDATE path records that change and its actual previous preference.
 IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM sec.preferred_source_processing_sets
                            WHERE filing_id=NEW.filing_id) THEN RETURN NEW; END IF;
 SELECT processing_family,processing_set_id INTO prior_family,prior_id
 FROM finance.filing WHERE filing_id=target_filing;
 rollback_event := nullif(current_setting('sec.rollback_publication_event',true),'')::bigint;
 IF rollback_event IS NOT NULL THEN
   SELECT * INTO reverting FROM sec.source_publication_history WHERE event_id=rollback_event;
   IF reverting.event_id IS NULL OR reverting.event_kind<>'publish'
      OR reverting.filing_id<>target_filing OR TG_OP='INSERT'
      OR reverting.new_preference IS DISTINCT FROM OLD.processing_set_id
      OR reverting.old_preference IS DISTINCT FROM (CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.processing_set_id END)
      OR EXISTS(SELECT 1 FROM sec.source_publication_history WHERE reverts_event_id=rollback_event) THEN
     RAISE EXCEPTION 'Rollback must revert an active publication event for this filing' USING ERRCODE='23514';
   END IF;
 END IF;
 -- A historical restoration is allowed only after the exact active-event
 -- lineage above has been proved. A normal new publication needs this parser.
 IF rollback_event IS NULL AND TG_OP<>'DELETE' AND
    target.configuration->'parser_revisions' IS DISTINCT FROM
      CASE target.versions->>'tables' WHEN 'tables-v5' THEN
        '{"facts":"numeric-tag-boundaries-v3","reports":"corrected-visible-layout-boundaries-v4","tables":"registrant-numeric-cell-validation-v2"}'::jsonb
      ELSE
        '{"facts":"numeric-tag-boundaries-v3","reports":"reviewed-original-source-boundaries-v4","tables":"registrant-numeric-cell-validation-v2","chunks":"token-boundary-overlap-v2"}'::jsonb END THEN
   RAISE EXCEPTION 'New publication requires current parser revisions; historical receipts remain immutable' USING ERRCODE='23514';
 END IF;
 INSERT INTO sec.source_publication_history
  (filing_id,old_preference,new_preference,previous_served_family,previous_served_set_id,event_kind,reverts_event_id)
 VALUES (target_filing,CASE WHEN TG_OP='INSERT' THEN NULL ELSE OLD.processing_set_id END,
         CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.processing_set_id END,prior_family,prior_id,
         CASE WHEN rollback_event IS NULL THEN 'publish' ELSE 'rollback' END,rollback_event);
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
