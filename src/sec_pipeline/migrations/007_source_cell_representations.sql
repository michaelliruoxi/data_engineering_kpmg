-- V3 source markers are represented literally, never fabricated as numeric facts.
-- Existing V1/V2 rows and retained JSON are unchanged; their schema remains v1.
ALTER TABLE sec.financial_tables DROP CONSTRAINT financial_tables_relational_cells_version_check;
ALTER TABLE sec.financial_tables ADD CONSTRAINT financial_tables_relational_cells_version_check
    CHECK (relational_cells_version IS NULL OR relational_cells_version IN (1, 2));
ALTER TABLE sec.table_cells DROP CONSTRAINT table_cells_resolution_status_check;
ALTER TABLE sec.table_cells ADD CONSTRAINT table_cells_resolution_status_check
    CHECK (resolution_status IN ('resolved', 'unresolved', 'non_fact', 'source_only'));
ALTER TABLE sec.table_cells ADD CONSTRAINT table_cells_source_only_data_check
    CHECK (resolution_status <> 'source_only' OR kind = 'data');
ALTER TABLE sec.cell_fact_links DROP CONSTRAINT cell_fact_links_link_method_check;
ALTER TABLE sec.cell_fact_links ADD CONSTRAINT cell_fact_links_link_method_check
    CHECK (link_method IN ('inline_id', 'same_row_semantic_repeat', 'hidden_inline_id'));

CREATE TABLE sec.cell_source_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL,
    table_id uuid NOT NULL,
    cell_id uuid NOT NULL UNIQUE,
    source_document_id uuid NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    verification_version text NOT NULL CHECK (verification_version = 'source-marker-v1'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (cell_id, filing_id, table_id) REFERENCES sec.table_cells(id, filing_id, table_id) ON DELETE CASCADE,
    FOREIGN KEY (source_document_id, filing_id) REFERENCES sec.source_documents(id, filing_id)
);
CREATE INDEX cell_source_observations_table_idx ON sec.cell_source_observations(table_id);

CREATE FUNCTION sec.guard_cell_source_observation() RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NOT FOUND OR parent.relational_cells_version IS DISTINCT FROM 2 OR parent.structure->>'schema_version' IS DISTINCT FROM '2'
        OR parent.extraction_version <> 'tables-v3' OR parent.source_document_id <> NEW.source_document_id THEN
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
CREATE TRIGGER cell_source_observations_guard BEFORE INSERT OR UPDATE OR DELETE ON sec.cell_source_observations
    FOR EACH ROW EXECUTE FUNCTION sec.guard_cell_source_observation();
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
    IF NOT FOUND OR ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version <> 'tables-v3') OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3')) IS NOT TRUE THEN
        RAISE EXCEPTION 'Cell requires a schema-v1 relational table in the same filing' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key) <> 1 THEN
        RAISE EXCEPTION 'Cell key must identify exactly one retained source cell' USING ERRCODE = '23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key;
    expected_kind := CASE source_cell->>'classification' WHEN 'resolved' THEN 'data'
        WHEN 'unresolved' THEN 'data' WHEN 'source_only_marker' THEN 'data' WHEN 'resolved_label' THEN 'label' ELSE source_cell->>'classification' END;
    expected_status := CASE source_cell->>'classification' WHEN 'resolved' THEN 'resolved'
        WHEN 'resolved_label' THEN 'resolved' WHEN 'unresolved' THEN 'unresolved' WHEN 'source_only_marker' THEN 'source_only' ELSE 'non_fact' END;
    IF NEW.resolution_status = 'source_only' AND (parent.relational_cells_version <> 2
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
    IF NEW.link_method = 'hidden_inline_id' AND (parent.relational_cells_version IS DISTINCT FROM 2
        OR parent.extraction_version <> 'tables-v3' OR parent.structure->>'schema_version' IS DISTINCT FROM '2'
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

CREATE OR REPLACE FUNCTION sec.validate_table_cell_consistency(target_table uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE parent sec.financial_tables%ROWTYPE;
BEGIN
    SELECT * INTO parent FROM sec.financial_tables WHERE id = target_table;
    IF NOT FOUND OR parent.relational_cells_version IS NULL THEN RETURN; END IF;
    IF ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version <> 'tables-v3')
        OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3')) IS NOT TRUE
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
END;
$$;

CREATE OR REPLACE FUNCTION sec.validate_processing_set() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    fact_count bigint; table_count bigint; report_count bigint; chunk_count bigint;
    link_count bigint; section_count bigint; source_counts jsonb;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT count(*) INTO fact_count FROM sec.financial_facts
        WHERE filing_id=NEW.filing_id AND source_document_id=NEW.fact_source_document_id
          AND extraction_version=NEW.fact_extraction_version;
    SELECT count(*) INTO table_count FROM sec.financial_tables
        WHERE filing_id=NEW.filing_id AND source_document_id=NEW.table_source_document_id
          AND extraction_version=NEW.table_extraction_version;
    SELECT count(*) INTO report_count FROM sec.reports
        WHERE filing_id=NEW.filing_id AND id=NEW.report_id;
    SELECT count(*) INTO chunk_count FROM sec.chunks
        WHERE filing_id=NEW.filing_id AND report_id=NEW.report_id AND chunking_version=NEW.chunking_version;
    IF (NEW.fact_source_document_id IS NOT NULL AND fact_count = 0)
       OR (NEW.table_source_document_id IS NOT NULL AND table_count = 0)
       OR (NEW.report_id IS NOT NULL AND report_count = 0)
       OR (NEW.chunking_version IS NOT NULL AND chunk_count = 0) THEN
        RAISE EXCEPTION 'Selected stages must reference existing nonempty artifact scopes; omit absent stages explicitly'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM sec.financial_tables AS t
        JOIN sec.financial_table_facts AS l ON l.table_id=t.id
        JOIN sec.financial_facts AS f ON f.id=l.fact_id
        WHERE t.source_document_id=NEW.table_source_document_id
          AND t.extraction_version=NEW.table_extraction_version
          AND (f.source_document_id IS DISTINCT FROM NEW.fact_source_document_id
               OR f.extraction_version IS DISTINCT FROM NEW.fact_extraction_version)
    ) THEN
        RAISE EXCEPTION 'Selected tables link facts outside the selected fact source/version'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM sec.financial_tables AS t
        WHERE t.source_document_id=NEW.table_source_document_id
          AND t.extraction_version=NEW.table_extraction_version
          AND t.structure ? 'fact_scope'
          AND ((t.structure#>>'{fact_scope,xml_document_id}') IS DISTINCT FROM NEW.fact_source_document_id::text
               OR (t.structure#>>'{fact_scope,facts_version}') IS DISTINCT FROM NEW.fact_extraction_version)
    ) THEN
        RAISE EXCEPTION 'Table fact_scope does not match selected facts'
            USING ERRCODE = '23514';
    END IF;
    SELECT count(*) INTO link_count FROM sec.financial_table_facts AS l
        JOIN sec.financial_tables AS t ON t.id=l.table_id
        WHERE t.source_document_id=NEW.table_source_document_id AND t.extraction_version=NEW.table_extraction_version;
    SELECT count(*) INTO section_count FROM sec.report_sections WHERE report_id=NEW.report_id;
    NEW.counts := jsonb_build_object('facts',fact_count,'tables',table_count,'reports',report_count,
        'chunks',chunk_count,'table_fact_links',link_count,'sections',section_count);
    IF NEW.table_extraction_version = 'tables-v3' THEN
        IF EXISTS (SELECT 1 FROM sec.financial_tables t
            WHERE t.filing_id=NEW.filing_id AND t.source_document_id=NEW.table_source_document_id
              AND t.extraction_version=NEW.table_extraction_version
              AND (t.relational_cells_version IS DISTINCT FROM 2
                   OR t.structure->>'schema_version' IS DISTINCT FROM '2')) THEN
            RAISE EXCEPTION 'Every selected V3 table must retain schema-v2 relational cells' USING ERRCODE = '23514';
        END IF;
        SELECT jsonb_build_object(
            'financial_cells', count(*) FILTER (WHERE c.kind='data'),
            'linked_financial_cells', count(*) FILTER (WHERE c.kind='data' AND c.resolution_status='resolved'),
            'source_only_marker_cells', count(*) FILTER (WHERE c.resolution_status='source_only'),
            'hidden_nil_cells', count(*) FILTER (WHERE EXISTS (SELECT 1 FROM sec.cell_fact_links l WHERE l.cell_id=c.id AND l.link_method='hidden_inline_id')),
            'unresolved_cells', count(*) FILTER (WHERE c.resolution_status='unresolved'))
        INTO source_counts FROM sec.table_cells c JOIN sec.financial_tables t ON t.id=c.table_id
        WHERE t.filing_id=NEW.filing_id AND t.source_document_id=NEW.table_source_document_id
          AND t.extraction_version=NEW.table_extraction_version;
        IF NEW.configuration->'source_cell_counts' IS DISTINCT FROM source_counts
            OR (source_counts->>'unresolved_cells')::bigint <> 0
            OR NEW.validation->'checks'->'source_only_markers_verified' IS DISTINCT FROM 'true'::jsonb
            OR NEW.validation->'checks'->'hidden_nil_bindings_verified' IS DISTINCT FROM 'true'::jsonb
            OR NEW.validation->'checks'->'financial_cell_inventory_complete' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION 'V3 cell counts and source representation checks must match selected database rows' USING ERRCODE = '23514';
        END IF;
        NEW.counts := NEW.counts || source_counts;
    END IF;
    RETURN NEW;
END;
$$;
CREATE CONSTRAINT TRIGGER cell_source_observations_complete AFTER UPDATE OR DELETE ON sec.cell_source_observations
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sec.check_table_cell_consistency_trigger();
CREATE TRIGGER cell_source_observations_no_managed_truncate BEFORE TRUNCATE ON sec.cell_source_observations
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_managed_cells_truncate();

CREATE VIEW sec.statement_source_observations AS
SELECT c.*, o.source_document_id AS observation_source_document_id, o.source_sha256 AS observation_source_sha256,
       o.verification_version AS observation_verification_version, o.evidence AS source_only_evidence
FROM sec.statement_cells_all_versions c JOIN sec.cell_source_observations o ON o.cell_id=c.cell_id;
COMMENT ON TABLE sec.cell_source_observations IS
    'Literal financial markers with verified absence of supported direct/hidden numeric binding. No amount, zero, or nil is inferred; numeric facts are never fabricated.';
COMMENT ON VIEW sec.statement_source_observations IS
    'Source-only financial markers across explicit extraction versions, including their unchanged source location and absence-verification evidence.';
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
        GRANT SELECT ON sec.cell_source_observations, sec.statement_source_observations TO sec_reader;
    END IF;
END $$;


-- Retain original issuer identity and association precedence for both verified table profiles.
CREATE OR REPLACE VIEW sec.issuer_filing_history AS
WITH associations AS (
    SELECT f.company_id AS current_company_id, f.id AS filing_id,
           'direct'::text AS relationship, 0 AS priority,
           NULL::uuid AS issuer_succession_id, NULL::date AS succession_effective_date,
           NULL::uuid AS processing_set_id, NULL::text AS evidence_url,
           NULL::text AS evidence_sha256, NULL::text AS evidence_local_path,
           NULL::timestamptz AS evidence_retrieved_at
    FROM sec.filings f
    UNION ALL
    SELECT s.successor_company_id, f.id, 'predecessor', 2,
           s.id, s.effective_date, NULL::uuid,
           s.evidence_url, s.evidence_sha256, s.evidence_local_path, s.evidence_retrieved_at
    FROM sec.issuer_successions s
    JOIN sec.filings f ON f.company_id=s.predecessor_company_id AND f.filing_date <= s.effective_date
    UNION ALL
    SELECT current_issuer.id, f.id, 'co_registrant', 1,
           NULL::uuid, NULL::date, selection.id,
           source.source_url, source.sha256, source.local_path, source.retrieved_at
    FROM sec.filing_processing_sets selection
    JOIN sec.filings f ON f.id=selection.filing_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(selection.configuration->'index_registrants')='array'
             THEN selection.configuration->'index_registrants' ELSE '[]'::jsonb END
    ) AS registrant(value)
    JOIN sec.companies current_issuer ON current_issuer.cik=registrant.value->>'cik'
    JOIN sec.source_documents source ON source.filing_id=f.id
        AND source.role='filing_index' AND source.source_url=f.source_url
    WHERE selection.validation_status='passed' AND selection.table_extraction_version IN ('tables-v2', 'tables-v3')
      AND selection.validation->'checks'->'index_registrants_verified'='true'::jsonb
), chosen AS (
    SELECT DISTINCT ON (current_company_id, filing_id) * FROM associations
    ORDER BY current_company_id, filing_id, priority,
             succession_effective_date DESC NULLS LAST, issuer_succession_id NULLS LAST,
             processing_set_id NULLS LAST
)
SELECT current_issuer.id AS current_company_id, current_issuer.cik AS current_cik,
       current_issuer.name AS current_company_name,
       original_issuer.id AS original_company_id, original_issuer.cik AS original_cik,
       original_issuer.name AS original_company_profile_name,
       f.company_name_as_filed, f.id AS filing_id, f.accession_number, f.form,
       f.filing_date, f.reporting_period_end, f.source_url AS filing_source_url,
       chosen.relationship, chosen.issuer_succession_id, chosen.succession_effective_date,
       chosen.processing_set_id, chosen.evidence_url, chosen.evidence_sha256,
       chosen.evidence_local_path, chosen.evidence_retrieved_at
FROM chosen
JOIN sec.companies current_issuer ON current_issuer.id=chosen.current_company_id
JOIN sec.filings f ON f.id=chosen.filing_id
JOIN sec.companies original_issuer ON original_issuer.id=f.company_id;

COMMENT ON VIEW sec.issuer_filing_history IS
    'One row per current issuer and original filing. Direct ownership takes precedence, then verified tables-v2/tables-v3 index co-registration, then an explicit predecessor link through its effective date. Original filing identities and names are retained; history is not transitive.';
