-- Relational cells are opt-in for legacy tables; schema-v1 loaders enable them.
ALTER TABLE sec.financial_tables ADD COLUMN relational_cells_version integer
    CHECK (relational_cells_version IS NULL OR relational_cells_version = 1);

CREATE TABLE sec.table_cells (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL,
    table_id uuid NOT NULL,
    cell_key text NOT NULL CHECK (btrim(cell_key) <> ''),
    row_index integer NOT NULL CHECK (row_index >= 0),
    column_index integer NOT NULL CHECK (column_index >= 0),
    row_span integer NOT NULL CHECK (row_span > 0),
    column_span integer NOT NULL CHECK (column_span > 0),
    kind text NOT NULL CHECK (kind IN ('data', 'label', 'header', 'blank', 'formatting')),
    display_text text NOT NULL,
    source_locator jsonb NOT NULL CHECK (jsonb_typeof(source_locator) = 'object'),
    resolution_status text NOT NULL CHECK (resolution_status IN ('resolved', 'unresolved', 'non_fact')),
    unresolved_reason text,
    row_heading text NOT NULL,
    column_headings jsonb NOT NULL CHECK (jsonb_typeof(column_headings) = 'array'),
    column_period jsonb CHECK (column_period IS NULL OR jsonb_typeof(column_period) = 'object'),
    is_total boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (table_id, cell_key),
    UNIQUE (table_id, row_index, column_index),
    UNIQUE (id, filing_id, table_id),
    FOREIGN KEY (table_id, filing_id) REFERENCES sec.financial_tables(id, filing_id) ON DELETE CASCADE,
    CHECK ((resolution_status = 'unresolved' AND unresolved_reason IS NOT NULL AND btrim(unresolved_reason) <> '')
        OR (resolution_status <> 'unresolved' AND unresolved_reason IS NULL)),
    CHECK (coalesce(jsonb_typeof(source_locator->'xpath') = 'string'
        AND btrim(source_locator->>'xpath') <> '', false))
);

CREATE TABLE sec.cell_fact_links (
    filing_id uuid NOT NULL,
    table_id uuid NOT NULL,
    cell_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    reference_order integer NOT NULL CHECK (reference_order >= 0),
    link_method text NOT NULL CHECK (link_method IN ('inline_id', 'same_row_semantic_repeat')),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cell_id, fact_id),
    UNIQUE (cell_id, reference_order),
    FOREIGN KEY (cell_id, filing_id, table_id)
        REFERENCES sec.table_cells(id, filing_id, table_id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, filing_id) REFERENCES sec.financial_facts(id, filing_id)
);
CREATE INDEX table_cells_filing_idx ON sec.table_cells(filing_id);
CREATE INDEX cell_fact_links_table_idx ON sec.cell_fact_links(table_id);
CREATE INDEX cell_fact_links_fact_idx ON sec.cell_fact_links(fact_id, filing_id);

CREATE FUNCTION sec.table_structure_cells(layout jsonb) RETURNS SETOF jsonb
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT cell.value
    FROM jsonb_array_elements(layout->'rows') AS row_item(value)
    CROSS JOIN LATERAL jsonb_array_elements(row_item.value) AS cell(value);
$$;

CREATE FUNCTION sec.guard_table_cell_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NOT FOUND OR parent.relational_cells_version IS DISTINCT FROM 1 OR parent.structure->>'schema_version' IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'Cell requires a schema-v1 relational table in the same filing' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key) <> 1 THEN
        RAISE EXCEPTION 'Cell key must identify exactly one retained source cell' USING ERRCODE = '23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key;
    expected_kind := CASE source_cell->>'classification' WHEN 'resolved' THEN 'data'
        WHEN 'unresolved' THEN 'data' WHEN 'resolved_label' THEN 'label' ELSE source_cell->>'classification' END;
    expected_status := CASE source_cell->>'classification' WHEN 'resolved' THEN 'resolved'
        WHEN 'resolved_label' THEN 'resolved' WHEN 'unresolved' THEN 'unresolved' ELSE 'non_fact' END;
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
CREATE TRIGGER table_cells_evidence_guard BEFORE INSERT OR UPDATE OR DELETE ON sec.table_cells
    FOR EACH ROW EXECUTE FUNCTION sec.guard_table_cell_evidence();

CREATE FUNCTION sec.guard_cell_fact_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER cell_fact_links_evidence_guard BEFORE INSERT OR UPDATE OR DELETE ON sec.cell_fact_links
    FOR EACH ROW EXECUTE FUNCTION sec.guard_cell_fact_evidence();

CREATE FUNCTION sec.derive_legacy_table_membership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        INSERT INTO sec.financial_table_facts(filing_id, table_id, fact_id)
        VALUES (NEW.filing_id, NEW.table_id, NEW.fact_id) ON CONFLICT DO NOTHING;
    END IF;
    IF TG_OP IN ('DELETE', 'UPDATE') THEN
        DELETE FROM sec.financial_table_facts m WHERE m.table_id = OLD.table_id AND m.fact_id = OLD.fact_id
        AND NOT EXISTS (SELECT 1 FROM sec.cell_fact_links l WHERE l.table_id = OLD.table_id AND l.fact_id = OLD.fact_id);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER cell_fact_links_legacy_membership AFTER INSERT OR UPDATE OR DELETE ON sec.cell_fact_links
    FOR EACH ROW EXECUTE FUNCTION sec.derive_legacy_table_membership();

CREATE FUNCTION sec.validate_table_cell_consistency(target_table uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE parent sec.financial_tables%ROWTYPE;
BEGIN
    SELECT * INTO parent FROM sec.financial_tables WHERE id = target_table;
    IF NOT FOUND OR parent.relational_cells_version IS NULL THEN RETURN; END IF;
    IF parent.structure->>'schema_version' IS DISTINCT FROM '1'
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
END;
$$;

CREATE FUNCTION sec.check_table_cell_consistency_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'financial_tables' THEN
        PERFORM sec.validate_table_cell_consistency(CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END);
    ELSE
        PERFORM sec.validate_table_cell_consistency(CASE WHEN TG_OP = 'DELETE' THEN OLD.table_id ELSE NEW.table_id END);
        IF TG_OP = 'UPDATE' AND OLD.table_id IS DISTINCT FROM NEW.table_id THEN
            PERFORM sec.validate_table_cell_consistency(OLD.table_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
-- First load is checked by the parent INSERT/relational_cells_version UPDATE.
-- Once complete, source-key and reference-order guards cannot add extra cells
-- or links; only removals/updates need another deferred whole-table check.
CREATE CONSTRAINT TRIGGER table_cells_complete AFTER UPDATE OR DELETE ON sec.table_cells
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sec.check_table_cell_consistency_trigger();
CREATE CONSTRAINT TRIGGER cell_fact_links_complete AFTER UPDATE OR DELETE ON sec.cell_fact_links
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sec.check_table_cell_consistency_trigger();
CREATE CONSTRAINT TRIGGER financial_tables_cells_complete AFTER INSERT OR UPDATE OR DELETE ON sec.financial_tables
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sec.check_table_cell_consistency_trigger();
CREATE CONSTRAINT TRIGGER financial_table_membership_complete AFTER UPDATE OR DELETE ON sec.financial_table_facts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sec.check_table_cell_consistency_trigger();

CREATE FUNCTION sec.guard_derived_table_membership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    IF EXISTS (SELECT 1 FROM sec.financial_tables WHERE id = NEW.table_id AND relational_cells_version IS NOT NULL)
        AND NOT EXISTS (SELECT 1 FROM sec.cell_fact_links WHERE table_id = NEW.table_id AND fact_id = NEW.fact_id) THEN
        RAISE EXCEPTION 'Managed table membership must derive from an accepted cell link' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER financial_table_facts_derived_guard BEFORE INSERT OR UPDATE ON sec.financial_table_facts
    FOR EACH ROW EXECUTE FUNCTION sec.guard_derived_table_membership();

-- Do not allow a caller to disable relational checks or change retained layout
-- beneath cells. Create a new extraction version for changed source output.
CREATE FUNCTION sec.guard_managed_table_layout() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.relational_cells_version IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'Relational table layout is immutable; use a new extraction version' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER financial_tables_managed_layout BEFORE UPDATE ON sec.financial_tables
    FOR EACH ROW EXECUTE FUNCTION sec.guard_managed_table_layout();

CREATE FUNCTION sec.guard_cell_linked_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sec.lock_processing_filing(OLD.filing_id);
    IF EXISTS (SELECT 1 FROM sec.cell_fact_links WHERE fact_id = OLD.id)
        AND (TG_OP = 'DELETE' OR NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'A cell-linked fact is immutable; use a new extraction version' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER financial_facts_cell_evidence BEFORE UPDATE OR DELETE ON sec.financial_facts
    FOR EACH ROW EXECUTE FUNCTION sec.guard_cell_linked_fact();

CREATE TRIGGER table_cells_no_sealed_truncate BEFORE TRUNCATE ON sec.table_cells
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER cell_fact_links_no_sealed_truncate BEFORE TRUNCATE ON sec.cell_fact_links
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();

CREATE FUNCTION sec.guard_managed_cells_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM sec.financial_tables WHERE relational_cells_version IS NOT NULL) THEN
        RAISE EXCEPTION 'Truncation would bypass relational table completeness; delete table records in a transaction instead'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER table_cells_no_managed_truncate BEFORE TRUNCATE ON sec.table_cells
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_managed_cells_truncate();
CREATE TRIGGER cell_fact_links_no_managed_truncate BEFORE TRUNCATE ON sec.cell_fact_links
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_managed_cells_truncate();
CREATE TRIGGER financial_table_facts_no_managed_truncate BEFORE TRUNCATE ON sec.financial_table_facts
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_managed_cells_truncate();

CREATE VIEW sec.statement_cells_all_versions AS
SELECT c.id AS cell_id, c.filing_id, c.table_id, c.cell_key, c.row_index, c.column_index,
    c.row_span, c.column_span, c.kind, c.display_text, c.source_locator, c.resolution_status,
    c.unresolved_reason, c.row_heading, c.column_headings, c.column_period, c.is_total, c.created_at,
    t.table_key, t.statement_type, t.source_document_id, t.extraction_version,
    s.source_url, s.sha256 AS source_sha256, s.local_path,
    f.accession_number, f.form, f.filing_date, f.reporting_period_end,
    co.id AS company_id, co.cik, co.name AS company_name, co.ticker,
    refs.fact_count, refs.linked_facts
FROM sec.table_cells c
JOIN sec.financial_tables t ON (t.id, t.filing_id) = (c.table_id, c.filing_id)
JOIN sec.source_documents s ON (s.id, s.filing_id) = (t.source_document_id, t.filing_id)
JOIN sec.filings f ON f.id = c.filing_id
JOIN sec.companies co ON co.id = f.company_id
CROSS JOIN LATERAL (
    SELECT count(*) AS fact_count, coalesce(jsonb_agg(jsonb_build_object(
        'fact_id', ff.id, 'occurrence_key', ff.occurrence_key,
        'concept_namespace', ff.concept_namespace, 'concept_name', ff.concept_name,
        'label', ff.label, 'numeric_value', ff.numeric_value::text, 'raw_value', ff.raw_value,
        'is_nil', ff.is_nil, 'reported_decimals', ff.reported_decimals, 'reported_precision', ff.reported_precision,
        'period_kind', ff.period_kind, 'period_start', ff.period_start, 'period_end', ff.period_end,
        'instant_date', ff.instant_date, 'unit', ff.unit, 'dimensions', ff.dimensions,
        'source_document_id', ff.source_document_id, 'extraction_version', ff.extraction_version,
        'source_url', fs.source_url, 'source_reference', ff.source_reference,
        'reference_order', l.reference_order, 'link_method', l.link_method, 'evidence', l.evidence
    ) ORDER BY l.reference_order), '[]'::jsonb) AS linked_facts
    FROM sec.cell_fact_links l
    JOIN sec.financial_facts ff ON (ff.id, ff.filing_id) = (l.fact_id, l.filing_id)
    JOIN sec.source_documents fs ON (fs.id, fs.filing_id) = (ff.source_document_id, ff.filing_id)
    WHERE l.cell_id = c.id
) refs;

CREATE VIEW sec.statement_cells AS
SELECT sc.processing_set_id, sc.set_key, sc.validation_status, sc.coverage,
       sc.stage_coverage, cells.*
FROM sec.statement_cells_all_versions cells
JOIN sec.statement_catalog sc ON sc.table_id = cells.table_id;

COMMENT ON TABLE sec.table_cells IS
    'One row per physical anchor cell, including headers/blanks; spans do not create duplicate cells.';
COMMENT ON TABLE sec.cell_fact_links IS
    'Accepted source-verified references only; multiple facts per cell retain source order, and unresolved candidates are never links.';
COMMENT ON VIEW sec.statement_cells IS
    'One row per cell in the preferred processing set; linked_facts is an ordered array, numeric_value strings preserve exact decimals.';
COMMENT ON COLUMN sec.financial_tables.relational_cells_version IS
    'NULL for legacy JSON-only tables; 1 means cells, accepted links, JSON evidence, and legacy table memberships are consistency-checked.';
