-- Add the flagged V8 identities (facts-v1 / tables-v8 / report-v4 / paragraph-v2).
-- tables-v8 extracts the tables printed after a cash flow statement (a closing
-- cash reconciliation, supplemental disclosures) as further segments, and
-- records company tagging errors: a filer's tag that contradicts its printed
-- page. The flagged cell stays unlinked, the defect is stored with what the page
-- prints and what the tag claims, and readers never receive the flagged fact.
-- Every function below is the definition from 016 with V8 added; V4-V7 rows,
-- receipts, counts and rules are unchanged.
ALTER TABLE sec.source_processing_sets DROP CONSTRAINT source_processing_sets_versions_check;
ALTER TABLE sec.source_processing_sets ADD CONSTRAINT source_processing_sets_versions_check CHECK (
 versions IN (
 '{"facts":"facts-v1","tables":"tables-v4","reports":"report-v1","chunks":"paragraph-v1"}'::jsonb,
 '{"facts":"facts-v1","tables":"tables-v5","reports":"report-v2","chunks":"paragraph-v1"}'::jsonb,
 '{"facts":"facts-v1","tables":"tables-v6","reports":"report-v3","chunks":"paragraph-v2"}'::jsonb,
 '{"facts":"facts-v1","tables":"tables-v7","reports":"report-v4","chunks":"paragraph-v2"}'::jsonb,
 '{"facts":"facts-v1","tables":"tables-v8","reports":"report-v4","chunks":"paragraph-v2"}'::jsonb
 ));

-- A flagged cell shows the printed number and is linked to no fact.
ALTER TABLE sec.table_cells DROP CONSTRAINT table_cells_resolution_status_check;
ALTER TABLE sec.table_cells ADD CONSTRAINT table_cells_resolution_status_check
    CHECK (resolution_status IN ('resolved','unresolved','non_fact','source_only','review_required','source_defect'));
ALTER TABLE sec.table_cells ADD CONSTRAINT table_cells_source_defect_data_check
    CHECK (resolution_status<>'source_defect' OR kind='data');

CREATE TABLE sec.source_tagging_defects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filing_id uuid NOT NULL, document_set_id uuid NOT NULL,
    defect_key text NOT NULL CHECK (defect_key ~ '^tables-v8:tagging-defect:[0-9a-f]{64}$'),
    fact_id uuid NOT NULL, occurrence_id uuid NOT NULL, inline_id text NOT NULL CHECK (btrim(inline_id)<>''),
    inline_document_id uuid NOT NULL, table_id uuid, cell_id uuid,
    code text NOT NULL CHECK (code IN ('fact_period_differs_from_column','opening_balance_date_not_prior_day','instant_context_under_duration_header','printed_number_outside_tag')),
    rule_version text NOT NULL CHECK (rule_version='company-tagging-defects-v1'),
    expected jsonb NOT NULL CHECK (jsonb_typeof(expected)='object'), observed jsonb NOT NULL CHECK (jsonb_typeof(observed)='object'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_set_id, defect_key), UNIQUE (id, filing_id), UNIQUE (cell_id),
    CHECK ((table_id IS NULL) = (cell_id IS NULL)), CHECK ((cell_id IS NULL) = (code='printed_number_outside_tag')),
    CHECK (expected<>observed),
    FOREIGN KEY (document_set_id, filing_id) REFERENCES sec.source_document_sets(id, filing_id),
    FOREIGN KEY (document_set_id, inline_document_id) REFERENCES sec.source_document_set_members(document_set_id, source_document_id),
    FOREIGN KEY (occurrence_id, document_set_id, fact_id, filing_id)
        REFERENCES sec.fact_source_occurrences(id, document_set_id, fact_id, filing_id),
    FOREIGN KEY (cell_id, filing_id, table_id) REFERENCES sec.table_cells(id, filing_id, table_id)
);
CREATE TABLE sec.source_processing_tagging_defects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), processing_set_id uuid NOT NULL, filing_id uuid NOT NULL, defect_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (processing_set_id, defect_id),
    FOREIGN KEY (processing_set_id, filing_id) REFERENCES sec.source_processing_sets(id, filing_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (defect_id, filing_id) REFERENCES sec.source_tagging_defects(id, filing_id)
);
CREATE INDEX source_tagging_defects_fact_idx ON sec.source_tagging_defects(fact_id);
CREATE INDEX source_processing_tagging_defects_defect_idx ON sec.source_processing_tagging_defects(defect_id);

CREATE FUNCTION sec.guard_source_tagging_defect() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ds sec.source_document_sets%ROWTYPE; fact sec.financial_facts%ROWTYPE; occurrence sec.fact_source_occurrences%ROWTYPE;
        cell sec.table_cells%ROWTYPE; parent sec.financial_tables%ROWTYPE; retained jsonb; period jsonb;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO ds FROM sec.source_document_sets WHERE id=NEW.document_set_id AND filing_id=NEW.filing_id;
    SELECT * INTO fact FROM sec.financial_facts WHERE id=NEW.fact_id AND filing_id=NEW.filing_id;
    SELECT * INTO occurrence FROM sec.fact_source_occurrences
        WHERE id=NEW.occurrence_id AND document_set_id=NEW.document_set_id AND fact_id=NEW.fact_id;
    period := CASE fact.period_kind WHEN 'instant' THEN jsonb_build_object('instant',fact.instant_date::text)
        WHEN 'duration' THEN jsonb_build_object('startDate',fact.period_start::text,'endDate',fact.period_end::text)
        ELSE jsonb_build_object('forever','') END;
    IF ds.id IS NULL OR fact.id IS NULL OR occurrence.id IS NULL OR fact.source_document_id<>ds.xml_document_id
        OR fact.extraction_version<>'facts-v1' OR fact.is_nil OR fact.unit IS NULL
        OR NEW.inline_id IS DISTINCT FROM fact.occurrence_key OR NEW.inline_id IS DISTINCT FROM occurrence.inline_id
        OR NEW.inline_document_id IS DISTINCT FROM occurrence.html_document_id
        OR NEW.evidence->>'inline_xpath' IS DISTINCT FROM occurrence.evidence->>'inline_xpath'
        OR NEW.observed->'period' IS DISTINCT FROM period
        OR NEW.observed->>'concept' IS DISTINCT FROM ('{'||fact.concept_namespace||'}'||fact.concept_name)
        OR NEW.observed->>'raw_value' IS DISTINCT FROM fact.raw_value THEN
        RAISE EXCEPTION 'A tagging defect must name its exact stored fact and source occurrence' USING ERRCODE='23514';
    END IF;
    IF NEW.code='printed_number_outside_tag' AND (jsonb_typeof(NEW.expected->'printed_number') IS DISTINCT FROM 'string'
        OR jsonb_typeof(NEW.observed->'tagged_number') IS DISTINCT FROM 'string'
        OR NEW.expected->>'printed_number'=NEW.observed->>'tagged_number'
        OR strpos(NEW.expected->>'printed_number', NEW.observed->>'tagged_number')=0) THEN
        RAISE EXCEPTION 'A partially tagged number must retain the printed and the tagged text' USING ERRCODE='23514';
    END IF;
    IF NEW.cell_id IS NULL THEN
        IF EXISTS(SELECT 1 FROM sec.source_processing_sets p WHERE p.document_set_id=NEW.document_set_id AND p.versions->>'tables'='tables-v8') THEN
            RAISE EXCEPTION 'Tagging defects are sealed by their V8 processing set' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO cell FROM sec.table_cells WHERE id=NEW.cell_id AND table_id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT * INTO parent FROM sec.financial_tables WHERE id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT value INTO retained FROM sec.table_structure_cells(parent.structure) value WHERE value->>'cell_id'=cell.cell_key;
    IF cell.id IS NULL OR parent.id IS NULL OR sec.table_scope_is_sealed(NEW.table_id) OR cell.resolution_status<>'source_defect'
        OR parent.extraction_version<>'tables-v8' OR parent.source_document_id<>NEW.inline_document_id
        OR retained->'tagging_defect'->>'defect_key' IS DISTINCT FROM NEW.defect_key
        OR retained->'tagging_defect'->>'code' IS DISTINCT FROM NEW.code
        OR retained->'tagging_defect'->>'inline_id' IS DISTINCT FROM NEW.inline_id
        OR retained->'tagging_defect'->>'inline_xpath' IS DISTINCT FROM NEW.evidence->>'inline_xpath'
        OR retained->'tagging_defect'->'cell_tags' IS DISTINCT FROM NEW.evidence->'cell_tags'
        OR NEW.evidence->>'cell_xpath' IS DISTINCT FROM retained->>'source_xpath' THEN
        RAISE EXCEPTION 'A cell tagging defect must match its flagged V8 cell exactly' USING ERRCODE='23514';
    END IF;
    IF NEW.code<>'printed_number_outside_tag' AND (NEW.expected->'period' IS DISTINCT FROM cell.column_period
        OR NEW.observed->'period' IS NOT DISTINCT FROM NEW.expected->'period'
        OR NEW.observed->'period' IS NOT DISTINCT FROM jsonb_build_object('instant',NEW.expected->>'instant')) THEN
        RAISE EXCEPTION 'A period tagging defect must contradict the printed column' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER source_tagging_defects_check BEFORE INSERT ON sec.source_tagging_defects
    FOR EACH ROW EXECUTE FUNCTION sec.guard_source_tagging_defect();
CREATE TRIGGER source_tagging_defects_immutable BEFORE UPDATE OR DELETE ON sec.source_tagging_defects
    FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_immutable();
CREATE TRIGGER source_processing_tagging_defects_immutable BEFORE UPDATE OR DELETE ON sec.source_processing_tagging_defects
    FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_immutable();
CREATE TRIGGER source_processing_tagging_defects_closed_members BEFORE INSERT ON sec.source_processing_tagging_defects
    FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_member_insert();
CREATE TRIGGER source_tagging_defects_v4_no_truncate BEFORE TRUNCATE ON sec.source_tagging_defects
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_v4_truncate();
CREATE TRIGGER source_processing_tagging_defects_v4_no_truncate BEFORE TRUNCATE ON sec.source_processing_tagging_defects
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_v4_truncate();

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
    IF NOT FOUND OR ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version NOT IN ('tables-v3','tables-v4','tables-v5','tables-v6','tables-v7','tables-v8')) OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3') OR (parent.relational_cells_version = 3 AND parent.structure->>'schema_version' = '3' AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6','tables-v7','tables-v8'))) IS NOT TRUE THEN
        RAISE EXCEPTION 'Cell requires a schema-v1 relational table in the same filing' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key) <> 1 THEN
        RAISE EXCEPTION 'Cell key must identify exactly one retained source cell' USING ERRCODE = '23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key;
    expected_kind := CASE source_cell->>'classification' WHEN 'resolved' THEN 'data'
        WHEN 'unresolved' THEN 'data' WHEN 'source_only_marker' THEN 'data' WHEN 'source_conflict' THEN 'data' WHEN 'source_defect' THEN 'data' WHEN 'resolved_label' THEN 'label' ELSE source_cell->>'classification' END;
    expected_status := CASE source_cell->>'classification' WHEN 'resolved' THEN 'resolved'
        WHEN 'resolved_label' THEN 'resolved' WHEN 'unresolved' THEN 'unresolved' WHEN 'source_only_marker' THEN 'source_only' WHEN 'source_conflict' THEN 'review_required' WHEN 'source_defect' THEN 'source_defect' ELSE 'non_fact' END;
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
    IF NEW.resolution_status='source_defect' AND (parent.extraction_version<>'tables-v8'
        OR parent.relational_cells_version<>3 OR NEW.kind<>'data'
        OR jsonb_typeof(source_cell->'tagging_defect') IS DISTINCT FROM 'object'
        OR source_cell->'tagging_defect'->>'rule_version' IS DISTINCT FROM 'company-tagging-defects-v1'
        OR coalesce(source_cell->'tagging_defect'->>'defect_key','') !~ '^tables-v8:tagging-defect:[0-9a-f]{64}$'
        OR coalesce(source_cell->'tagging_defect'->>'code','') NOT IN ('fact_period_differs_from_column','opening_balance_date_not_prior_day','instant_context_under_duration_header')
        OR jsonb_typeof(source_cell->'tagging_defect'->'cell_tags') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'tagging_defect'->'cell_tags')=0) THEN
        RAISE EXCEPTION 'Flagged cells require one recorded V8 company tagging defect' USING ERRCODE='23514';
    END IF;
    IF NEW.resolution_status<>'source_defect' AND source_cell ? 'tagging_defect' THEN
        RAISE EXCEPTION 'Only flagged cells may carry a company tagging defect' USING ERRCODE='23514';
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
            OR (parent.relational_cells_version=3 AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6','tables-v7','tables-v8') AND parent.structure->>'schema_version'='3')) IS NOT TRUE
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
        OR (parent.relational_cells_version=3 AND parent.structure->>'schema_version'='3' AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6','tables-v7','tables-v8'))) IS NOT TRUE OR parent.source_document_id <> NEW.source_document_id THEN
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
    IF ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version NOT IN ('tables-v3','tables-v4','tables-v5','tables-v6','tables-v7','tables-v8'))
        OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3') OR (parent.relational_cells_version = 3 AND parent.structure->>'schema_version' = '3' AND parent.extraction_version IN ('tables-v4','tables-v5','tables-v6','tables-v7','tables-v8'))) IS NOT TRUE
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
        IF EXISTS(SELECT 1 FROM sec.table_cells c JOIN LATERAL
            (SELECT v FROM sec.table_structure_cells(parent.structure) v WHERE v->>'cell_id'=c.cell_key) source ON true
            WHERE c.table_id=target_table AND (c.resolution_status='source_defect') IS DISTINCT FROM (source.v ? 'tagging_defect')) THEN
            RAISE EXCEPTION 'Exactly flagged cells retain company tagging defects' USING ERRCODE='23514';
        END IF;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION sec.validate_source_processing_set() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ds sec.source_document_sets%ROWTYPE; fact_count bigint; document_count bigint; table_count bigint;
        report_count bigint; chunk_count bigint; cell_count bigint; accepted_count bigint; source_only_count bigint;
        review_count bigint; issue_count bigint; target_table uuid; defect_cell_count bigint; defect_count bigint;
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
    IF NEW.versions->>'tables' IN ('tables-v5','tables-v6','tables-v7','tables-v8') AND (
        NEW.semantic_checks->'primary_section_boundaries' IS DISTINCT FROM 'true'::jsonb
        OR NEW.source_checks->'numeric_tag_boundaries_verified' IS DISTINCT FROM 'true'::jsonb
        OR NEW.configuration->'versions' IS DISTINCT FROM NEW.versions
        OR NEW.configuration->>'extraction_profile' IS DISTINCT FROM
           CASE NEW.versions->>'tables' WHEN 'tables-v5' THEN 'corrected-layout-v5' WHEN 'tables-v6' THEN 'reviewed-layout-v6' WHEN 'tables-v7' THEN 'audited-layout-v7' ELSE 'flagged-layout-v8' END
        OR NEW.configuration->>'section_validation_policy' IS DISTINCT FROM
           CASE NEW.versions->>'tables' WHEN 'tables-v5' THEN 'original-heading-v1' ELSE 'source-boundaries-v2' END
        OR NEW.configuration->'parser_revisions' IS DISTINCT FROM
           CASE NEW.versions->>'tables' WHEN 'tables-v5' THEN
             '{"facts":"numeric-tag-boundaries-v3","reports":"corrected-visible-layout-boundaries-v4","tables":"registrant-numeric-cell-validation-v2"}'::jsonb
           WHEN 'tables-v6' THEN
             '{"facts":"numeric-tag-boundaries-v3","reports":"reviewed-original-source-boundaries-v4","tables":"registrant-numeric-cell-validation-v2","chunks":"token-boundary-overlap-v2"}'::jsonb
           WHEN 'tables-v7' THEN
             '{"facts":"numeric-tag-boundaries-v3","reports":"audited-section-metadata-v1","tables":"statement-continuation-inventory-v1","chunks":"token-boundary-overlap-v2"}'::jsonb
           ELSE
             '{"facts":"numeric-tag-boundary-defects-v1","reports":"audited-section-metadata-v1","tables":"cash-statement-followers-tagging-defects-v1","chunks":"token-boundary-overlap-v2"}'::jsonb END) THEN
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
        count(*) FILTER(WHERE c.resolution_status='source_only'), count(*) FILTER(WHERE c.resolution_status='review_required'),
        count(*) FILTER(WHERE c.resolution_status='source_defect')
    INTO cell_count,accepted_count,source_only_count,review_count,defect_cell_count
    FROM sec.source_processing_tables member JOIN sec.table_cells c ON c.table_id=member.table_id WHERE member.processing_set_id=NEW.id;
    SELECT count(*) INTO issue_count FROM sec.source_processing_issues WHERE processing_set_id=NEW.id;
    SELECT count(*) INTO defect_count FROM sec.source_processing_tagging_defects WHERE processing_set_id=NEW.id;
    -- Only V8 records company tagging errors: its selected defects are exactly
    -- the document set's fact-level defects and those of its own flagged
    -- cells, each flagged cell has exactly one, and none is a review issue.
    IF (NEW.versions->>'tables'<>'tables-v8' AND (defect_cell_count<>0 OR defect_count<>0))
        OR (NEW.versions->>'tables'='tables-v8' AND (review_count<>0 OR issue_count<>0
            OR EXISTS((SELECT d.id FROM sec.source_tagging_defects d WHERE d.document_set_id=ds.id AND (d.cell_id IS NULL
                         OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.table_cells c ON c.table_id=member.table_id
                                   WHERE member.processing_set_id=NEW.id AND c.id=d.cell_id))
                       EXCEPT SELECT defect_id FROM sec.source_processing_tagging_defects WHERE processing_set_id=NEW.id)
                      UNION ALL
                      (SELECT defect_id FROM sec.source_processing_tagging_defects WHERE processing_set_id=NEW.id
                       EXCEPT SELECT d.id FROM sec.source_tagging_defects d WHERE d.document_set_id=ds.id))
            OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.table_cells c ON c.table_id=member.table_id
                WHERE member.processing_set_id=NEW.id AND c.resolution_status='source_defect'
                  AND (SELECT count(*) FROM sec.source_processing_tagging_defects link JOIN sec.source_tagging_defects d ON d.id=link.defect_id
                       WHERE link.processing_set_id=NEW.id AND d.cell_id=c.id)<>1)
            OR EXISTS(SELECT 1 FROM sec.source_processing_tagging_defects link JOIN sec.source_tagging_defects d ON d.id=link.defect_id
                JOIN sec.cell_fact_links accepted ON accepted.fact_id=d.fact_id
                JOIN sec.source_processing_tables member ON member.table_id=accepted.table_id AND member.processing_set_id=NEW.id
                WHERE link.processing_set_id=NEW.id))) THEN
        RAISE EXCEPTION 'V8 company tagging defects must match their flagged cells and document-set scope exactly' USING ERRCODE='23514';
    END IF;
    IF cell_count=0 OR cell_count<>accepted_count+source_only_count+review_count+defect_cell_count OR (review_count=0) IS DISTINCT FROM (issue_count=0)
        OR NEW.semantic_checks->'period_header_consistency' IS DISTINCT FROM to_jsonb(issue_count=0)
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.semantic_checks) c WHERE c.value='false'::jsonb AND c.key<>'period_header_consistency') THEN
        RAISE EXCEPTION 'Every V4 financial cell and semantic exception must be explicitly accounted for' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO chunk_count FROM sec.source_processing_reports member JOIN sec.chunks c
        ON c.report_id=member.report_id AND c.chunking_version=member.chunking_version WHERE member.processing_set_id=NEW.id;
    NEW.validation_status:=CASE WHEN issue_count=0 THEN 'passed' ELSE 'review_required' END;
    NEW.counts:=jsonb_build_object('inline_documents',document_count,'facts',fact_count,'tables',table_count,'reports',report_count,'chunks',chunk_count,
        'financial_cells',cell_count,'accepted_cells',accepted_count,'source_only_cells',source_only_count,
        'review_required_cells',review_count,'quality_issues',issue_count,'unaccounted_cells',0)
        || CASE WHEN NEW.versions->>'tables'='tables-v8'
                THEN jsonb_build_object('source_defect_cells',defect_cell_count,'tagging_defects',defect_count)
                ELSE '{}'::jsonb END;
    RETURN NEW;
END; $$;

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
      OR target.versions->>'tables' NOT IN ('tables-v5','tables-v6','tables-v7','tables-v8')
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
      (CASE target.versions->>'tables' WHEN 'tables-v5' THEN
        '{"facts":"numeric-tag-boundaries-v3","reports":"corrected-visible-layout-boundaries-v4","tables":"registrant-numeric-cell-validation-v2"}'::jsonb
      WHEN 'tables-v6' THEN
        '{"facts":"numeric-tag-boundaries-v3","reports":"reviewed-original-source-boundaries-v4","tables":"registrant-numeric-cell-validation-v2","chunks":"token-boundary-overlap-v2"}'::jsonb
      WHEN 'tables-v7' THEN
        '{"facts":"numeric-tag-boundaries-v3","reports":"audited-section-metadata-v1","tables":"statement-continuation-inventory-v1","chunks":"token-boundary-overlap-v2"}'::jsonb
      ELSE
        '{"facts":"numeric-tag-boundary-defects-v1","reports":"audited-section-metadata-v1","tables":"cash-statement-followers-tagging-defects-v1","chunks":"token-boundary-overlap-v2"}'::jsonb END) THEN
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

CREATE OR REPLACE VIEW sec.source_fact_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.source_validation_status,p.validation_status,p.coverage,
    fact.*,occ.html_document_id,occ.inline_id,occ.source_xpath,occ.evidence AS occurrence_evidence,occ.fragments,
    s.source_url AS inline_source_url,s.sha256 AS inline_source_sha256,
    f.accession_number,f.form,f.company_name_as_filed,issuer.cik,issuer.name AS company_name,
    coalesce(issues.issue_count,0) AS quality_issue_count,coalesce(issues.issue_codes,ARRAY[]::text[]) AS quality_issue_codes,
    coalesce(defects.defect_count,0) AS tagging_defect_count,coalesce(defects.defect_codes,ARRAY[]::text[]) AS tagging_defect_codes
FROM sec.source_processing_sets p JOIN sec.source_document_sets ds ON ds.id=p.document_set_id
JOIN sec.financial_facts fact ON fact.source_document_id=ds.xml_document_id AND fact.extraction_version=p.versions->>'facts'
JOIN sec.fact_source_occurrences occ ON occ.document_set_id=ds.id AND occ.fact_id=fact.id
JOIN sec.source_documents s ON s.id=occ.html_document_id JOIN sec.filings f ON f.id=p.filing_id JOIN sec.companies issuer ON issuer.id=f.company_id
LEFT JOIN LATERAL (SELECT count(*) AS issue_count,array_agg(q.code ORDER BY q.issue_key) AS issue_codes
    FROM sec.source_quality_issues q JOIN sec.cell_source_fact_bindings binding ON binding.id=q.binding_id
    JOIN sec.source_processing_issues selected ON selected.issue_id=q.id AND selected.processing_set_id=p.id
    WHERE binding.fact_id=fact.id) issues ON true
LEFT JOIN LATERAL (SELECT count(*) AS defect_count,array_agg(d.code ORDER BY d.defect_key) AS defect_codes
    FROM sec.source_tagging_defects d JOIN sec.source_processing_tagging_defects selected
      ON selected.defect_id=d.id AND selected.processing_set_id=p.id
    WHERE d.fact_id=fact.id) defects ON true;

CREATE OR REPLACE VIEW sec.analysis_ready_statement_cells AS
SELECT c.* FROM sec.source_statement_cells c
JOIN finance.filing served ON served.filing_id=c.filing_id
 AND served.processing_family='source' AND served.processing_set_id=c.processing_set_id
WHERE c.validation_status='passed' AND c.quality_issue_count=0 AND c.resolution_status NOT IN ('review_required','source_defect');

CREATE OR REPLACE VIEW sec.analysis_ready_facts AS
SELECT c.* FROM sec.source_fact_catalog c
JOIN finance.filing served ON served.filing_id=c.filing_id
 AND served.processing_family='source' AND served.processing_set_id=c.processing_set_id
WHERE c.validation_status='passed' AND c.quality_issue_count=0 AND c.tagging_defect_count=0;

CREATE OR REPLACE VIEW finance.metric_evidence AS
WITH candidates AS (
 SELECT f.filing_id,f.company_id,f.cik,f.ticker,f.processing_family,f.processing_set_id,f.validation_status,
        d.metric_key,d.statement_type,d.period_kind AS expected_period_kind,d.unit_kind,d.mapping_version,
        a.id AS fact_id,a.source_document_id,a.extraction_version,a.occurrence_key,a.concept_name,a.concept_namespace,
        a.numeric_value,a.raw_value,a.is_nil,a.period_kind,a.period_start,
        coalesce(a.period_end,a.instant_date) AS period_end,a.instant_date,a.unit,a.dimensions,a.source_reference,
        CASE WHEN a.period_kind='instant' THEN 'instant:'||to_char(a.instant_date,'YYYY-MM-DD')
             WHEN a.period_kind='duration' THEN 'duration:'||to_char(a.period_start,'YYYY-MM-DD')||':'||to_char(a.period_end,'YYYY-MM-DD') END AS period_id,
        substring(a.unit#>>'{numerator,0}' FROM '^\{https?://www[.]xbrl[.]org/2003/iso4217\}([A-Z]{3})$') AS currency,
        s.source_url,s.sha256 AS source_sha256,
        CASE WHEN a.concept_name <> 'RevenueFromContractWithCustomerExcludingAssessedTax' THEN true ELSE EXISTS (
          SELECT 1 FROM finance.statement st JOIN sec.table_cells tc ON tc.table_id=st.statement_id
          JOIN sec.cell_fact_links link ON link.cell_id=tc.id AND link.fact_id=a.id
          WHERE st.filing_id=f.filing_id AND st.statement_type='income_statement'
            AND lower(btrim(regexp_replace(tc.row_heading,'\s+',' ','g'))) IN
                ('revenue','revenues','total revenue','total revenues','net sales','net revenue','net revenues','sales')
        ) END AS top_line_verified
 FROM finance.filing f JOIN sec.financial_facts a
   ON a.filing_id=f.filing_id AND a.source_document_id=f.fact_source_document_id AND a.extraction_version=f.fact_extraction_version
 JOIN finance.metric_definition d ON a.concept_name=ANY(d.concepts)
 JOIN sec.source_documents s ON s.id=a.source_document_id
 WHERE a.concept_namespace ~ '^https?://(fasb[.]org|xbrl[.]us)/us-gaap/'
   -- A fact flagged as a company tagging error in the served set is never a metric.
   AND NOT EXISTS(SELECT 1 FROM sec.source_processing_tagging_defects selected
     JOIN sec.source_tagging_defects d ON d.id=selected.defect_id
     WHERE f.processing_family='source' AND selected.processing_set_id=f.processing_set_id AND d.fact_id=a.id)
), checked AS (
 SELECT *, CASE
   WHEN dimensions <> '[]'::jsonb THEN 'dimensional_fact'
   WHEN source_reference#>>'{entity,scheme}' NOT IN ('http://www.sec.gov/CIK','https://www.sec.gov/CIK')
     OR source_reference#>>'{entity,scheme}' IS NULL
     OR coalesce(source_reference#>>'{entity,identifier}','') !~ '^[0-9]{1,10}$'
     OR lpad(source_reference#>>'{entity,identifier}',10,'0') IS DISTINCT FROM cik THEN 'different_or_unknown_issuer'
   WHEN period_kind IS DISTINCT FROM expected_period_kind OR period_id IS NULL THEN 'wrong_period_kind'
   WHEN currency IS NULL OR CASE WHEN jsonb_typeof(unit->'numerator')='array' THEN jsonb_array_length(unit->'numerator') ELSE 0 END <> 1
     OR (unit_kind='currency' AND unit->'denominator' IS DISTINCT FROM '[]'::jsonb)
     OR (unit_kind='currency_per_share' AND (CASE WHEN jsonb_typeof(unit->'denominator')='array' THEN jsonb_array_length(unit->'denominator') ELSE 0 END<>1
       OR coalesce(unit#>>'{denominator,0}','') !~ '^\{https?://www[.]xbrl[.]org/2003/instance\}shares$')) THEN 'unsupported_unit'
   WHEN NOT top_line_verified THEN 'unverified_total_revenue'
   WHEN is_nil THEN 'nil_fact'
   WHEN numeric_value IS NULL THEN 'non_numeric_fact'
   WHEN metric_key IN ('capital_expenditures','dividends_paid') AND numeric_value < 0 THEN 'unexpected_outflow_sign'
   ELSE NULL END AS exclusion_reason
 FROM candidates
)
SELECT *, exclusion_reason IS NULL AS eligible FROM checked;

COMMENT ON TABLE sec.source_tagging_defects IS 'Company tagging errors recorded by tables-v8: the printed text and the tagged fact that contradicts it. Nothing is repaired; flagged facts are kept out of analysis_ready_* and finance metrics for the served set.';
COMMENT ON VIEW sec.analysis_ready_facts IS 'Passed facts for the single served source processing set, without flagged company tagging errors. Use source_fact_catalog for explicit historical auditing.';
COMMENT ON VIEW sec.analysis_ready_statement_cells IS 'Passed cells for the single served source processing set; review-required and flagged (source_defect) cells are excluded. Use source_statement_cells for explicit historical auditing.';
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
        GRANT SELECT ON sec.source_tagging_defects, sec.source_processing_tagging_defects TO sec_reader;
    END IF;
END $$;
