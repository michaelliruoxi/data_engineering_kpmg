-- Preserve immutable history and distinguish restorations from new publications.
ALTER TABLE sec.source_publication_history
 ADD COLUMN event_kind text NOT NULL DEFAULT 'legacy' CHECK(event_kind IN ('legacy','publish','rollback')),
 ADD COLUMN reverts_event_id bigint UNIQUE REFERENCES sec.source_publication_history(event_id),
 ADD CONSTRAINT publication_rollback_event CHECK ((event_kind='rollback') = (reverts_event_id IS NOT NULL));
-- Pre-migration entries did not distinguish restoration from publication. Do
-- not guess their intent; only new events support automatic chained rollback.
ALTER TABLE sec.source_publication_history ALTER COLUMN event_kind SET DEFAULT 'publish';

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
 INSERT INTO sec.source_publication_history
  (filing_id,old_preference,new_preference,previous_served_family,previous_served_set_id,event_kind,reverts_event_id)
 VALUES (target_filing,CASE WHEN TG_OP='INSERT' THEN NULL ELSE OLD.processing_set_id END,
         CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.processing_set_id END,prior_family,prior_id,
         CASE WHEN rollback_event IS NULL THEN 'publish' ELSE 'rollback' END,rollback_event);
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;

-- Default analysis views resolve the same single serving selection as finance.*.
-- The source_fact_catalog/source_statement_cells views remain explicit history.
CREATE OR REPLACE VIEW sec.analysis_ready_statement_cells AS
SELECT c.* FROM sec.source_statement_cells c
JOIN finance.filing served ON served.filing_id=c.filing_id
 AND served.processing_family='source' AND served.processing_set_id=c.processing_set_id
WHERE c.validation_status='passed' AND c.quality_issue_count=0 AND c.resolution_status<>'review_required';
CREATE OR REPLACE VIEW sec.analysis_ready_facts AS
SELECT c.* FROM sec.source_fact_catalog c
JOIN finance.filing served ON served.filing_id=c.filing_id
 AND served.processing_family='source' AND served.processing_set_id=c.processing_set_id
WHERE c.validation_status='passed' AND c.quality_issue_count=0;

-- Copy definitions, rather than selecting from the replaced views (which
-- would introduce a dependency cycle). Historical citation lookups are explicit.
DO $$ BEGIN
 EXECUTE 'CREATE VIEW sec.source_chunk_catalog_history AS ' || pg_get_viewdef('sec.source_chunk_catalog'::regclass,true);
 EXECUTE 'CREATE VIEW sec.chunk_citations_history AS ' || pg_get_viewdef('sec.chunk_citations'::regclass,true);
END $$;
CREATE OR REPLACE VIEW sec.source_chunk_catalog AS
SELECT c.* FROM sec.source_chunk_catalog_history c
JOIN finance.filing served ON served.filing_id=c.filing_id
 AND served.processing_family='source' AND served.processing_set_id=c.processing_set_id;
CREATE OR REPLACE VIEW sec.chunk_citations AS
SELECT c.* FROM sec.chunk_citations_history c
WHERE EXISTS (
 SELECT 1 FROM finance.filing served
 JOIN sec.source_processing_reports selected ON served.processing_family='source'
  AND selected.processing_set_id=served.processing_set_id
 WHERE served.filing_id=c.filing_id AND selected.report_id=c.report_id
  AND selected.chunking_version=c.chunking_version
) OR EXISTS (
 SELECT 1 FROM finance.filing served
 JOIN sec.filing_processing_sets selected ON served.processing_family='legacy'
  AND selected.id=served.processing_set_id
 WHERE served.filing_id=c.filing_id AND selected.report_id=c.report_id
  AND selected.chunking_version=c.chunking_version
) OR (
 -- Preserve the original raw citation API for unversioned legacy reports.
 -- Corrected reports never qualify, including before their set is sealed.
 c.extraction_version NOT IN ('report-v2','report-v3')
 AND NOT EXISTS(SELECT 1 FROM sec.source_processing_reports p WHERE p.report_id=c.report_id)
 AND NOT EXISTS(SELECT 1 FROM sec.filing_processing_sets p WHERE p.report_id=c.report_id)
);
COMMENT ON VIEW sec.analysis_ready_facts IS 'Passed facts for the single served source processing set. Use source_fact_catalog for explicit historical auditing.';
COMMENT ON VIEW sec.analysis_ready_statement_cells IS 'Passed cells for the single served source processing set. Use source_statement_cells for explicit historical auditing.';
COMMENT ON VIEW sec.source_chunk_catalog IS 'Chunks for the served source processing set; source_chunk_catalog_history retains every version.';
COMMENT ON VIEW sec.chunk_citations IS 'Served versioned chunks plus unbound legacy reports. Corrected reports require publication; chunk_citations_history exposes all versions for explicit auditing.';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
  GRANT SELECT ON sec.source_chunk_catalog_history,sec.chunk_citations_history TO sec_reader;
 END IF;
END $$;
