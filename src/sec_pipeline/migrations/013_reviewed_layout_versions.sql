-- Add reviewed V6 identities while preserving existing V4/V5 rows and receipts.
-- Changed source boundaries and token overlap never reuse historical report IDs.
ALTER TABLE sec.source_processing_sets DROP CONSTRAINT source_processing_sets_versions_check;
ALTER TABLE sec.source_processing_sets ADD CONSTRAINT source_processing_sets_versions_check CHECK (
 versions IN (
 '{"facts":"facts-v1","tables":"tables-v4","reports":"report-v1","chunks":"paragraph-v1"}'::jsonb,
 '{"facts":"facts-v1","tables":"tables-v5","reports":"report-v2","chunks":"paragraph-v1"}'::jsonb,
 '{"facts":"facts-v1","tables":"tables-v6","reports":"report-v3","chunks":"paragraph-v2"}'::jsonb
 ));

-- Report memberships must admit the new chunk identity as well as the exact
-- legacy version. validate_source_processing_set below binds each membership
-- to its processing set's complete version tuple.
ALTER TABLE sec.source_processing_reports
 DROP CONSTRAINT source_processing_reports_chunking_version_check;
ALTER TABLE sec.source_processing_reports
 ADD CONSTRAINT source_processing_reports_chunking_version_check
 CHECK (chunking_version IN ('paragraph-v1','paragraph-v2'));

CREATE OR REPLACE FUNCTION sec.guard_review_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cell sec.table_cells%ROWTYPE; parent sec.financial_tables%ROWTYPE; occurrence sec.fact_source_occurrences%ROWTYPE; retained jsonb;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO cell FROM sec.table_cells WHERE id=NEW.cell_id AND table_id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT * INTO parent FROM sec.financial_tables WHERE id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT * INTO occurrence FROM sec.fact_source_occurrences WHERE id=NEW.occurrence_id AND fact_id=NEW.fact_id
        AND document_set_id=NEW.document_set_id AND filing_id=NEW.filing_id;
    SELECT value INTO retained FROM sec.table_structure_cells(parent.structure) value WHERE value->>'cell_id'=cell.cell_key;
    IF sec.table_scope_is_sealed(NEW.table_id) OR cell.id IS NULL OR occurrence.id IS NULL
        OR cell.kind<>'data' OR cell.resolution_status<>'review_required' OR parent.extraction_version NOT IN ('tables-v4','tables-v5','tables-v6')
        OR parent.relational_cells_version IS DISTINCT FROM 3 OR parent.structure->>'schema_version' IS DISTINCT FROM '3'
        OR occurrence.html_document_id<>parent.source_document_id
        OR NEW.evidence IS DISTINCT FROM retained->'source_fact_references'->NEW.reference_order
        OR NEW.evidence->>'method' IS DISTINCT FROM 'inline_id'
        OR left(occurrence.source_xpath,length(cell.source_locator->>'xpath')+1) IS DISTINCT FROM (cell.source_locator->>'xpath')||'/'
        OR NEW.evidence->>'inline_document_id' IS DISTINCT FROM occurrence.html_document_id::text
        OR NEW.evidence->>'inline_xpath' IS DISTINCT FROM occurrence.evidence->>'inline_xpath'
        OR NEW.evidence->>'xml_id' IS DISTINCT FROM occurrence.inline_id
        OR EXISTS(SELECT 1 FROM sec.cell_fact_links WHERE cell_id=NEW.cell_id) THEN
        RAISE EXCEPTION 'Review binding requires exact unaccepted V4 cell/source occurrence evidence' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM jsonb_each(occurrence.evidence) e
        WHERE e.key IN ('concept','context_ref','unit_ref','unit','period','dimensions','raw_value','is_nil')
          AND NEW.evidence->e.key IS DISTINCT FROM e.value) THEN
        RAISE EXCEPTION 'Review binding cannot reinterpret the original XML fact' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION sec.guard_table_cell_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_table uuid;
    target_filing uuid;
    parent sec.financial_tables%ROWTYPE;
    source_cell jsonb;
    expected_kind text;
    expected_status text;
BEGIN
    target_table := CASE WHEN TG_OP = 'DELETE' THEN OLD.table_id ELSE NEW.table_id END;
    target_filing := CASE WHEN TG_OP = 'DELETE' THEN OLD.filing_id ELSE NEW.filing_id END;
    PERFORM sec.lock_processing_filing(target_filing);
    IF TG_OP = 'UPDATE' AND (NEW.table_id, NEW.filing_id) IS DISTINCT FROM (OLD.table_id, OLD.filing_id) THEN
        RAISE EXCEPTION 'Cell identity cannot move to another table or filing' USING ERRCODE = '23514';
    END IF;
    IF sec.table_scope_is_sealed(target_table) THEN
        IF TG_OP = 'INSERT' AND EXISTS (
            SELECT 1 FROM sec.table_cells c WHERE c.table_id = NEW.table_id AND c.cell_key = NEW.cell_key
            AND to_jsonb(c) - 'id' - 'created_at' = to_jsonb(NEW) - 'id' - 'created_at'
        ) THEN RETURN NEW;
        ELSIF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW;
        ELSE RAISE EXCEPTION 'Cells in a recorded processing set are immutable' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    SELECT * INTO parent FROM sec.financial_tables WHERE id = NEW.table_id AND filing_id = NEW.filing_id FOR SHARE;
    IF NOT FOUND OR ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version NOT IN ('tables-v3','tables-v4','tables-v5','tables-v6')) OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3') OR (parent.relational_cells_version = 3 AND parent.structure->>'schema_version' = '3' AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6'))) IS NOT TRUE THEN
        RAISE EXCEPTION 'Cell requires a schema-v1 relational table in the same filing' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key) <> 1 THEN
        RAISE EXCEPTION 'Cell key must identify exactly one retained source cell' USING ERRCODE = '23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key;
    expected_kind := CASE source_cell->>'classification' WHEN 'resolved' THEN 'data'
        WHEN 'unresolved' THEN 'data' WHEN 'source_only_marker' THEN 'data' WHEN 'source_conflict' THEN 'data' WHEN 'resolved_label' THEN 'label' ELSE source_cell->>'classification' END;
    expected_status := CASE source_cell->>'classification' WHEN 'resolved' THEN 'resolved'
        WHEN 'resolved_label' THEN 'resolved' WHEN 'unresolved' THEN 'unresolved' WHEN 'source_only_marker' THEN 'source_only' WHEN 'source_conflict' THEN 'review_required' ELSE 'non_fact' END;
    IF NEW.resolution_status = 'source_only' AND (parent.relational_cells_version NOT IN (2,3)
        OR NEW.kind <> 'data' OR NEW.display_text NOT IN ('-', '–', '—')
        OR source_cell ?| ARRAY['numeric_value', 'raw_value', 'is_nil']
        OR source_cell->'source_only_evidence'->>'verification_version' IS DISTINCT FROM 'source-marker-v1'
        OR source_cell->'source_only_evidence'->>'literal' IS DISTINCT FROM NEW.display_text
        OR source_cell->'source_only_evidence'->>'source_xpath' IS DISTINCT FROM source_cell->>'source_xpath'
        OR source_cell->'source_only_evidence'->>'html_sha256' IS DISTINCT FROM parent.structure->>'html_sha256'
        OR source_cell->'source_only_evidence'->>'binding_scope' IS DISTINCT FROM 'archived_dom_numeric_and_sec_hidden_bindings'
        OR source_cell->'source_only_evidence'->>'runtime_scope' IS DISTINCT FROM 'scripts_not_executed'
        OR jsonb_typeof(source_cell->'source_only_evidence'->'external_scripts') IS DISTINCT FROM 'array'
        OR source_cell->'source_only_evidence'->'direct_numeric_ids' IS DISTINCT FROM '[]'::jsonb
        OR source_cell->'source_only_evidence'->'hidden_binding_targets' IS DISTINCT FROM '[]'::jsonb) THEN
        RAISE EXCEPTION 'Source-only cells require explicit V3 absence evidence' USING ERRCODE = '23514';
    END IF;
    IF NEW.resolution_status <> 'source_only' AND source_cell ? 'source_only_evidence' THEN
        RAISE EXCEPTION 'Only source-only cells may retain absence evidence' USING ERRCODE = '23514';
    END IF;
    IF NEW.resolution_status='review_required' AND (parent.relational_cells_version<>3
        OR jsonb_typeof(source_cell->'source_fact_references') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'source_fact_references')<>1
        OR jsonb_typeof(source_cell->'quality_issues') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'quality_issues')=0
        OR jsonb_typeof(source_cell->'quality_issue_keys') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'quality_issue_keys')<>jsonb_array_length(source_cell->'quality_issues')) THEN
        RAISE EXCEPTION 'Review cells require explicit V4 source bindings and quality issues' USING ERRCODE='23514';
    END IF;
    IF NEW.resolution_status<>'review_required' AND (
        coalesce(jsonb_array_length(source_cell->'source_fact_references'),0)>0
        OR coalesce(jsonb_array_length(source_cell->'quality_issues'),0)>0
        OR coalesce(jsonb_array_length(source_cell->'quality_issue_keys'),0)>0) THEN
        RAISE EXCEPTION 'Only review-required cells may carry semantic conflict bindings' USING ERRCODE='23514';
    END IF;
    IF (NEW.row_index, NEW.column_index, NEW.row_span, NEW.column_span, NEW.kind, NEW.display_text,
        NEW.source_locator, NEW.resolution_status, NEW.unresolved_reason, NEW.row_heading,
        NEW.column_headings, NEW.column_period, NEW.is_total) IS DISTINCT FROM
       ((source_cell->>'row')::integer, (source_cell->>'column')::integer,
        (source_cell->>'rowspan')::integer, (source_cell->>'colspan')::integer, expected_kind,
        source_cell->>'display_text', jsonb_build_object('xpath', source_cell->>'source_xpath',
        'html_id', source_cell->'source_html_id'), expected_status, source_cell->>'unresolved_reason',
        source_cell->>'row_heading', source_cell->'column_headings', nullif(source_cell->'column_period', 'null'::jsonb),
        (source_cell->>'is_total')::boolean) THEN
        RAISE EXCEPTION 'Relational cell differs from retained table evidence' USING ERRCODE = '23514';
    END IF;
    IF (NEW.resolution_status = 'resolved' AND jsonb_array_length(source_cell->'fact_references') = 0)
        OR (NEW.resolution_status <> 'resolved' AND jsonb_array_length(source_cell->'fact_references') <> 0) THEN
        RAISE EXCEPTION 'Only resolved cells can have accepted fact references' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.table_cells c WHERE c.table_id = NEW.table_id AND c.id <> NEW.id
        AND c.cell_key <> NEW.cell_key
        AND int8range(c.row_index, c.row_index::bigint + c.row_span, '[)') && int8range(NEW.row_index, NEW.row_index::bigint + NEW.row_span, '[)')
        AND int8range(c.column_index, c.column_index::bigint + c.column_span, '[)') && int8range(NEW.column_index, NEW.column_index::bigint + NEW.column_span, '[)')) THEN
        RAISE EXCEPTION 'Relational cell spans overlap' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.guard_cell_fact_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_table uuid;
    target_filing uuid;
    cell sec.table_cells%ROWTYPE;
    parent sec.financial_tables%ROWTYPE;
    fact sec.financial_facts%ROWTYPE;
    expected jsonb;
    period jsonb;
BEGIN
    target_table := CASE WHEN TG_OP = 'DELETE' THEN OLD.table_id ELSE NEW.table_id END;
    target_filing := CASE WHEN TG_OP = 'DELETE' THEN OLD.filing_id ELSE NEW.filing_id END;
    PERFORM sec.lock_processing_filing(target_filing);
    IF TG_OP = 'UPDATE' AND (NEW.table_id, NEW.cell_id, NEW.filing_id, NEW.fact_id)
        IS DISTINCT FROM (OLD.table_id, OLD.cell_id, OLD.filing_id, OLD.fact_id) THEN
        RAISE EXCEPTION 'Fact-link identity cannot be reassigned' USING ERRCODE = '23514';
    END IF;
    IF sec.table_scope_is_sealed(target_table) THEN
        IF TG_OP = 'INSERT' AND EXISTS (SELECT 1 FROM sec.cell_fact_links l
            WHERE l.cell_id = NEW.cell_id AND l.fact_id = NEW.fact_id
            AND to_jsonb(l) - 'created_at' = to_jsonb(NEW) - 'created_at') THEN RETURN NEW;
        ELSIF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW;
        ELSE RAISE EXCEPTION 'Cell links in a recorded processing set are immutable' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    SELECT * INTO cell FROM sec.table_cells WHERE id = NEW.cell_id AND table_id = NEW.table_id AND filing_id = NEW.filing_id;
    IF NOT FOUND OR cell.resolution_status <> 'resolved' THEN
        RAISE EXCEPTION 'Accepted fact links require a resolved cell in the same filing' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO parent FROM sec.financial_tables WHERE id = cell.table_id FOR SHARE;
    SELECT c->'fact_references'->NEW.reference_order INTO expected
        FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = cell.cell_key;
    IF expected IS NULL OR NEW.evidence IS DISTINCT FROM expected OR NEW.link_method IS DISTINCT FROM expected->>'method'
        OR nullif(btrim(expected->>'inline_xpath'), '') IS NULL
        OR (NEW.link_method = 'same_row_semantic_repeat' AND nullif(btrim(expected->>'repeated_from_inline_xpath'), '') IS NULL) THEN
        RAISE EXCEPTION 'Fact-link evidence must match an accepted source reference' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO fact FROM sec.financial_facts WHERE id = NEW.fact_id AND filing_id = NEW.filing_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Fact belongs to another filing or does not exist' USING ERRCODE = '23503'; END IF;
    IF NEW.link_method = 'hidden_inline_id' AND (((parent.relational_cells_version=2 AND parent.extraction_version='tables-v3' AND parent.structure->>'schema_version'='2')
            OR (parent.relational_cells_version=3 AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6') AND parent.structure->>'schema_version'='3')) IS NOT TRUE
        OR fact.is_nil IS NOT TRUE OR expected->'is_nil' IS DISTINCT FROM 'true'::jsonb
        OR expected->>'binding_property' IS DISTINCT FROM '-sec-ix-hidden'
        OR expected->>'binding_target_id' IS DISTINCT FROM expected->>'xml_id'
        OR nullif(btrim(expected->>'binding_xpath'), '') IS NULL
        OR nullif(btrim(expected->>'binding_style'), '') IS NULL
        OR nullif(btrim(expected->>'hidden_container_xpath'), '') IS NULL) THEN
        RAISE EXCEPTION 'Hidden fact links require an explicit V3 nil binding' USING ERRCODE = '23514';
    END IF;
    period := CASE fact.period_kind
        WHEN 'instant' THEN jsonb_build_object('instant', fact.instant_date::text)
        WHEN 'duration' THEN jsonb_build_object('startDate', fact.period_start::text, 'endDate', fact.period_end::text)
        ELSE jsonb_build_object('forever', '') END;
    IF (fact.source_document_id::text, fact.extraction_version, fact.filing_id::text) IS DISTINCT FROM
        (expected#>>'{scope,xml_document_id}', expected#>>'{scope,facts_version}', expected#>>'{scope,filing_id}')
        OR expected->'scope' IS DISTINCT FROM parent.structure->'fact_scope'
        OR (fact.concept_namespace || '}' || fact.concept_name) IS DISTINCT FROM substring(expected->>'concept' FROM 2)
        OR fact.raw_value IS DISTINCT FROM (CASE WHEN fact.is_nil THEN NULL ELSE expected->>'raw_value' END)
        OR fact.unit IS DISTINCT FROM nullif(expected->'unit', 'null'::jsonb)
        OR period IS DISTINCT FROM expected->'period'
        OR fact.dimensions IS DISTINCT FROM expected->'dimensions'
        OR fact.is_nil IS DISTINCT FROM (expected->>'is_nil')::boolean
        OR fact.numeric_value IS DISTINCT FROM (CASE WHEN fact.is_nil OR fact.unit IS NULL THEN NULL ELSE (expected->>'raw_value')::numeric END)
        OR (fact.source_reference ? 'context_ref' AND fact.source_reference->>'context_ref' IS DISTINCT FROM expected->>'context_ref')
        OR (fact.source_reference ? 'unit_ref' AND fact.source_reference->>'unit_ref' IS DISTINCT FROM expected->>'unit_ref')
        OR (expected->>'xml_id' = fact.occurrence_key
            OR expected->>'xml_id' = ANY(ARRAY[fact.source_reference->>'element_id', fact.source_reference->>'xml_id',
                                              fact.source_reference->>'id', fact.source_reference->>'fact_id'])) IS NOT TRUE THEN
        RAISE EXCEPTION 'Stored fact differs from verified cell evidence or source/version scope' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.guard_cell_source_observation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent sec.financial_tables%ROWTYPE; cell sec.table_cells%ROWTYPE; source_cell jsonb; source_hash text;
BEGIN
    PERFORM sec.lock_processing_filing(CASE WHEN TG_OP='DELETE' THEN OLD.filing_id ELSE NEW.filing_id END);
    IF sec.table_scope_is_sealed(CASE WHEN TG_OP='DELETE' THEN OLD.table_id ELSE NEW.table_id END) THEN
        IF TG_OP='INSERT' AND EXISTS (SELECT 1 FROM sec.cell_source_observations o WHERE o.cell_id=NEW.cell_id
            AND to_jsonb(o)-'id'-'created_at'=to_jsonb(NEW)-'id'-'created_at') THEN RETURN NEW;
        ELSIF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW;
        ELSE RAISE EXCEPTION 'Source observations in a recorded processing set are immutable' USING ERRCODE='23514'; END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF TG_OP='UPDATE' AND (NEW.filing_id,NEW.table_id,NEW.cell_id) IS DISTINCT FROM (OLD.filing_id,OLD.table_id,OLD.cell_id) THEN
        RAISE EXCEPTION 'Source observations cannot move between cells' USING ERRCODE='23514';
    END IF;
    SELECT * INTO cell FROM sec.table_cells WHERE id=NEW.cell_id AND filing_id=NEW.filing_id AND table_id=NEW.table_id;
    IF NOT FOUND OR cell.kind <> 'data' OR cell.resolution_status <> 'source_only' THEN
        RAISE EXCEPTION 'Source observation requires a source-only financial cell' USING ERRCODE='23514';
    END IF;
    SELECT * INTO parent FROM sec.financial_tables WHERE id=NEW.table_id AND filing_id=NEW.filing_id FOR SHARE;
    IF NOT FOUND OR ((parent.relational_cells_version=2 AND parent.structure->>'schema_version'='2' AND parent.extraction_version='tables-v3')
        OR (parent.relational_cells_version=3 AND parent.structure->>'schema_version'='3' AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6'))) IS NOT TRUE OR parent.source_document_id <> NEW.source_document_id THEN
        RAISE EXCEPTION 'Source observation requires the exact V3 HTML table scope' USING ERRCODE='23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id'=cell.cell_key;
    SELECT sha256 INTO source_hash FROM sec.source_documents WHERE id=NEW.source_document_id AND filing_id=NEW.filing_id AND role='main_inline_xbrl_filing';
    IF NOT FOUND OR NEW.source_sha256 IS DISTINCT FROM source_hash
        OR NEW.source_sha256 IS DISTINCT FROM parent.structure->>'html_sha256'
        OR NEW.evidence IS DISTINCT FROM source_cell->'source_only_evidence'
        OR NEW.verification_version IS DISTINCT FROM NEW.evidence->>'verification_version'
        OR NEW.evidence->>'literal' IS DISTINCT FROM cell.display_text
        OR NEW.evidence->>'source_xpath' IS DISTINCT FROM cell.source_locator->>'xpath' THEN
        RAISE EXCEPTION 'Source observation differs from retained cell/source evidence' USING ERRCODE='23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.cell_fact_links WHERE cell_id=NEW.cell_id) THEN
        RAISE EXCEPTION 'Source-only observations cannot have numeric fact links' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.validate_table_cell_consistency(target_table uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE parent sec.financial_tables%ROWTYPE;
BEGIN
    SELECT * INTO parent FROM sec.financial_tables WHERE id = target_table;
    IF NOT FOUND OR parent.relational_cells_version IS NULL THEN RETURN; END IF;
    IF ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version NOT IN ('tables-v3','tables-v4','tables-v5','tables-v6'))
        OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3') OR (parent.relational_cells_version = 3 AND parent.structure->>'schema_version' = '3' AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6'))) IS NOT TRUE
        OR jsonb_typeof(parent.structure->'rows') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Managed cells require the schema-v1 table layout contract' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_cells WHERE table_id = target_table) <>
       (SELECT count(*) FROM sec.table_structure_cells(parent.structure)) THEN
        RAISE EXCEPTION 'Relational cells must cover every retained anchor cell' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.table_cells c
        JOIN LATERAL (SELECT v FROM sec.table_structure_cells(parent.structure) v WHERE v->>'cell_id' = c.cell_key) source ON true
        WHERE c.table_id = target_table AND
        (SELECT count(*) FROM sec.cell_fact_links l WHERE l.cell_id = c.id) <> jsonb_array_length(source.v->'fact_references')) THEN
        RAISE EXCEPTION 'Cell links must cover exactly the accepted source references' USING ERRCODE = '23514';
    END IF;
    IF EXISTS ((SELECT fact_id FROM sec.financial_table_facts WHERE table_id = target_table
                EXCEPT SELECT fact_id FROM sec.cell_fact_links WHERE table_id = target_table)
               UNION ALL
               (SELECT fact_id FROM sec.cell_fact_links WHERE table_id = target_table
                EXCEPT SELECT fact_id FROM sec.financial_table_facts WHERE table_id = target_table)) THEN
        RAISE EXCEPTION 'Legacy table membership must equal accepted cell links' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.table_cells c LEFT JOIN sec.cell_source_observations o ON o.cell_id=c.id
        WHERE c.table_id=target_table AND ((c.resolution_status='source_only') IS DISTINCT FROM (o.cell_id IS NOT NULL))) THEN
        RAISE EXCEPTION 'Exactly source-only cells require retained source observations' USING ERRCODE = '23514';
    END IF;
    IF parent.relational_cells_version=3 THEN
        PERFORM sec.validate_v4_review_scope(target_table);
        IF EXISTS(SELECT 1 FROM sec.table_cells c JOIN LATERAL
            (SELECT v FROM sec.table_structure_cells(parent.structure) v WHERE v->>'cell_id'=c.cell_key) source ON true
            WHERE c.table_id=target_table AND (
                (SELECT count(*) FROM sec.cell_source_fact_bindings b WHERE b.cell_id=c.id)<>
                    coalesce(jsonb_array_length(source.v->'source_fact_references'),0)
                OR (c.resolution_status='review_required') IS DISTINCT FROM
                    EXISTS(SELECT 1 FROM sec.cell_source_fact_bindings b WHERE b.cell_id=c.id)
                OR coalesce((SELECT jsonb_agg(q.issue_key ORDER BY q.issue_key) FROM sec.source_quality_issues q
                    JOIN sec.cell_source_fact_bindings b ON b.id=q.binding_id WHERE b.cell_id=c.id),'[]'::jsonb)
                    IS DISTINCT FROM coalesce((SELECT jsonb_agg(value ORDER BY value) FROM jsonb_array_elements_text(source.v->'quality_issue_keys')),'[]'::jsonb))) THEN
            RAISE EXCEPTION 'V4 source bindings/issues must cover exactly retained review cells' USING ERRCODE='23514';
        END IF;
    END IF;
END;
$$;

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
             '{"facts":"numeric-tag-boundaries-v2","reports":"corrected-visible-layout-boundaries-v3","tables":"registrant-numeric-cell-validation-v2"}'::jsonb
           ELSE
             '{"facts":"numeric-tag-boundaries-v2","reports":"reviewed-original-source-boundaries-v3","tables":"registrant-numeric-cell-validation-v2","chunks":"token-boundary-overlap-v2"}'::jsonb END) THEN
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


-- Keep the actual stored revision visible, including missing historical receipts.
-- Never infer an old receipt's parser revision from today's version policy.
CREATE VIEW finance.processing_revision AS
SELECT v.processing_family, v.processing_set_id, v.filing_id, v.set_key,
       (f.processing_family=v.processing_family AND f.processing_set_id=v.processing_set_id) AS is_served,
       CASE v.processing_family WHEN 'source' THEN s.versions
            ELSE l.configuration->'versions' END AS versions,
       CASE v.processing_family WHEN 'source' THEN s.configuration->'parser_revisions'
            ELSE l.configuration->'parser_revisions' END AS parser_revisions,
       CASE v.processing_family WHEN 'source' THEN s.configuration->>'extraction_profile'
            ELSE l.configuration->>'extraction_profile' END AS extraction_profile
FROM finance.filing_version v
LEFT JOIN finance.filing f ON f.filing_id=v.filing_id
LEFT JOIN sec.source_processing_sets s ON v.processing_family='source' AND s.id=v.processing_set_id
LEFT JOIN sec.filing_processing_sets l ON v.processing_family='legacy' AND l.id=v.processing_set_id;
COMMENT ON VIEW finance.processing_revision IS
 'Recorded parser revisions per immutable processing set; NULL means missing historical evidence, not the current parser. is_served identifies the current reader selection.';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
   GRANT SELECT ON finance.processing_revision TO sec_reader;
 END IF;
END; $$;
