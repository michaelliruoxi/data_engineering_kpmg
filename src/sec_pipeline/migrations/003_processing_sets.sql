-- Immutable selections reuse existing artifact identities and preserve independent
-- stage versions. Existing all-version citation views remain unchanged.
CREATE FUNCTION sec.valid_processing_validation(status text, detail jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF status NOT IN ('unvalidated', 'passed', 'failed')
       OR jsonb_typeof(detail) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF status = 'passed' THEN
        IF jsonb_typeof(detail->'checks') IS DISTINCT FROM 'object'
           OR detail->'checks' = '{}'::jsonb THEN RETURN false; END IF;
        RETURN NOT EXISTS (SELECT 1 FROM jsonb_each(detail->'checks') AS c
                           WHERE btrim(c.key) = '' OR c.value IS DISTINCT FROM 'true'::jsonb);
    END IF;
    RETURN true;
END;
$$;

CREATE TABLE sec.filing_processing_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    set_key text NOT NULL CHECK (btrim(set_key) <> ''),
    fact_source_document_id uuid,
    fact_extraction_version text CHECK (fact_extraction_version IS NULL OR btrim(fact_extraction_version) <> ''),
    table_source_document_id uuid,
    table_extraction_version text CHECK (table_extraction_version IS NULL OR btrim(table_extraction_version) <> ''),
    report_id uuid,
    chunking_version text CHECK (chunking_version IS NULL OR btrim(chunking_version) <> ''),
    coverage jsonb NOT NULL CHECK (jsonb_typeof(coverage) = 'object'),
    validation_status text NOT NULL,
    validation jsonb NOT NULL CHECK (sec.valid_processing_validation(validation_status, validation)),
    configuration jsonb NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    counts jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counts) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (filing_id, set_key),
    UNIQUE (id, filing_id),
    FOREIGN KEY (fact_source_document_id, filing_id) REFERENCES sec.source_documents(id, filing_id),
    FOREIGN KEY (table_source_document_id, filing_id) REFERENCES sec.source_documents(id, filing_id),
    FOREIGN KEY (report_id, filing_id) REFERENCES sec.reports(id, filing_id),
    CHECK ((fact_source_document_id IS NULL) = (fact_extraction_version IS NULL)),
    CHECK ((table_source_document_id IS NULL) = (table_extraction_version IS NULL)),
    CHECK (chunking_version IS NULL OR report_id IS NOT NULL),
    CHECK (fact_source_document_id IS NOT NULL OR table_source_document_id IS NOT NULL OR report_id IS NOT NULL),
    CHECK (coverage ?& ARRAY['facts', 'tables', 'reports', 'chunks']
           AND coverage - ARRAY['facts', 'tables', 'reports', 'chunks'] = '{}'::jsonb),
    CHECK (COALESCE(coverage->>'facts' IN ('absent','unknown','partial','complete'), false)
       AND COALESCE(coverage->>'tables' IN ('absent','unknown','partial','complete'), false)
       AND COALESCE(coverage->>'reports' IN ('absent','unknown','partial','complete'), false)
       AND COALESCE(coverage->>'chunks' IN ('absent','unknown','partial','complete'), false)),
    CHECK (((coverage->>'facts' = 'absent') = (fact_source_document_id IS NULL))
       AND ((coverage->>'tables' = 'absent') = (table_source_document_id IS NULL))
       AND ((coverage->>'reports' = 'absent') = (report_id IS NULL))
       AND ((coverage->>'chunks' = 'absent') = (chunking_version IS NULL)))
);
CREATE INDEX processing_sets_fact_scope_idx ON sec.filing_processing_sets(fact_source_document_id, fact_extraction_version);
CREATE INDEX processing_sets_table_scope_idx ON sec.filing_processing_sets(table_source_document_id, table_extraction_version);
CREATE INDEX processing_sets_report_idx ON sec.filing_processing_sets(report_id);

CREATE TABLE sec.preferred_processing_sets (
    filing_id uuid PRIMARY KEY REFERENCES sec.filings(id),
    processing_set_id uuid NOT NULL,
    selected_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (processing_set_id, filing_id) REFERENCES sec.filing_processing_sets(id, filing_id)
);

CREATE FUNCTION sec.lock_processing_filing(target_filing_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    -- Fixed snapshots can miss a set committed while waiting for this lock.
    -- All artifact writers use fresh read-committed snapshots; PostgreSQL's
    -- read-uncommitted setting has the same behavior. Read-only queries are unrestricted.
    IF current_setting('transaction_isolation') NOT IN ('read committed', 'read uncommitted') THEN
        RAISE EXCEPTION 'Artifact writes and processing selections require read committed isolation'
            USING ERRCODE = '0A000';
    END IF;
    PERFORM pg_advisory_xact_lock(170018, hashtext(target_filing_id::text));
    PERFORM 1 FROM sec.filings WHERE id = target_filing_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Processing selection filing does not exist' USING ERRCODE = '23503';
    END IF;
END;
$$;

CREATE FUNCTION sec.table_scope_is_sealed(target_table_id uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM sec.financial_tables AS t
        JOIN sec.filing_processing_sets AS p
          ON p.table_source_document_id = t.source_document_id
         AND p.table_extraction_version = t.extraction_version
        WHERE t.id = target_table_id
    );
$$;

CREATE FUNCTION sec.validate_processing_set() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    fact_count bigint; table_count bigint; report_count bigint; chunk_count bigint;
    link_count bigint; section_count bigint;
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
    RETURN NEW;
END;
$$;
CREATE TRIGGER processing_sets_validate BEFORE INSERT ON sec.filing_processing_sets
    FOR EACH ROW EXECUTE FUNCTION sec.validate_processing_set();

CREATE FUNCTION sec.protect_processing_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'Processing sets are immutable; create a new set_key and explicitly select it'
        USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER processing_sets_immutable BEFORE UPDATE OR DELETE ON sec.filing_processing_sets
    FOR EACH ROW EXECUTE FUNCTION sec.protect_processing_set();

CREATE FUNCTION sec.validate_preferred_processing_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    IF NOT EXISTS (SELECT 1 FROM sec.filing_processing_sets
                   WHERE id=NEW.processing_set_id AND filing_id=NEW.filing_id AND validation_status='passed') THEN
        RAISE EXCEPTION 'Preferred selection requires a passed processing set belonging to this filing'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER preferred_sets_validate BEFORE INSERT OR UPDATE ON sec.preferred_processing_sets
    FOR EACH ROW EXECUTE FUNCTION sec.validate_preferred_processing_set();

CREATE FUNCTION sec.processing_artifact_is_sealed(artifact_table text, artifact jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    CASE artifact_table
    WHEN 'financial_facts' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets
            WHERE fact_source_document_id=(artifact->>'source_document_id')::uuid
              AND fact_extraction_version=artifact->>'extraction_version');
    WHEN 'financial_tables' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets
            WHERE table_source_document_id=(artifact->>'source_document_id')::uuid
              AND table_extraction_version=artifact->>'extraction_version');
    WHEN 'reports' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets AS p JOIN sec.reports AS r ON r.id=p.report_id
            WHERE r.source_document_id=(artifact->>'source_document_id')::uuid
              AND r.extraction_version=artifact->>'extraction_version');
    WHEN 'report_sections' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets WHERE report_id=(artifact->>'report_id')::uuid);
    WHEN 'chunks' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets
            WHERE report_id=(artifact->>'report_id')::uuid AND chunking_version=artifact->>'chunking_version');
    WHEN 'financial_table_facts' THEN
        RETURN sec.table_scope_is_sealed((artifact->>'table_id')::uuid);
    WHEN 'source_documents' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets AS p
            LEFT JOIN sec.reports AS r ON r.id=p.report_id
            WHERE (artifact->>'id')::uuid IN (p.fact_source_document_id,p.table_source_document_id,r.source_document_id));
    ELSE RAISE EXCEPTION 'Unknown processing artifact table: %', artifact_table;
    END CASE;
END;
$$;

CREATE FUNCTION sec.guard_processing_artifact() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_data jsonb; new_data jsonb; target uuid; is_sealed boolean := false;
    replay boolean; identity_predicate text;
BEGIN
    IF TG_OP <> 'INSERT' THEN old_data := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN new_data := to_jsonb(NEW); END IF;
    FOR target IN SELECT DISTINCT value FROM (
        VALUES ((old_data->>'filing_id')::uuid), ((new_data->>'filing_id')::uuid)
    ) AS ids(value) WHERE value IS NOT NULL ORDER BY value LOOP
        PERFORM sec.lock_processing_filing(target);
    END LOOP;
    IF TG_OP <> 'INSERT' THEN
        is_sealed := sec.processing_artifact_is_sealed(TG_TABLE_NAME,old_data);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        is_sealed := is_sealed OR sec.processing_artifact_is_sealed(TG_TABLE_NAME,new_data);
    END IF;
    IF NOT is_sealed THEN
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    IF TG_OP='UPDATE' AND old_data IS NOT DISTINCT FROM new_data THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME='source_documents' AND TG_OP='UPDATE'
       AND old_data - ARRAY['local_path','retrieved_at','storage_uri','title','document_type'] = new_data - ARRAY['local_path','retrieved_at','storage_uri','title','document_type'] THEN
        RETURN NEW;
    END IF;
    IF TG_OP='INSERT' THEN
        identity_predicate := CASE TG_TABLE_NAME
            WHEN 'financial_facts' THEN 'x.source_document_id=($1->>''source_document_id'')::uuid AND x.extraction_version=$1->>''extraction_version'' AND x.occurrence_key=$1->>''occurrence_key'''
            WHEN 'financial_tables' THEN 'x.source_document_id=($1->>''source_document_id'')::uuid AND x.extraction_version=$1->>''extraction_version'' AND x.table_key=$1->>''table_key'''
            WHEN 'reports' THEN 'x.source_document_id=($1->>''source_document_id'')::uuid AND x.extraction_version=$1->>''extraction_version'''
            WHEN 'report_sections' THEN 'x.report_id=($1->>''report_id'')::uuid AND x.section_key=$1->>''section_key'''
            WHEN 'chunks' THEN 'x.report_id=($1->>''report_id'')::uuid AND x.chunking_version=$1->>''chunking_version'' AND x.chunk_index=($1->>''chunk_index'')::integer'
            WHEN 'financial_table_facts' THEN 'x.table_id=($1->>''table_id'')::uuid AND x.fact_id=($1->>''fact_id'')::uuid'
            ELSE 'x.id=($1->>''id'')::uuid' END;
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM sec.%I AS x WHERE %s AND to_jsonb(x) - ARRAY[''id'',''created_at'',''relational_cells_version''] = $1 - ARRAY[''id'',''created_at'',''relational_cells_version''])',
                       TG_TABLE_NAME, identity_predicate) INTO replay USING new_data;
        IF replay THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION 'Artifact scope is sealed by a processing set; use a new extraction/chunking version'
        USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER facts_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.financial_facts
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();
CREATE TRIGGER tables_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.financial_tables
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();
CREATE TRIGGER reports_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.reports
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();
CREATE TRIGGER chunks_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.chunks
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();
CREATE TRIGGER a_sections_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.report_sections
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();
CREATE TRIGGER links_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.financial_table_facts
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();
CREATE TRIGGER sources_processing_seal BEFORE INSERT OR UPDATE OR DELETE ON sec.source_documents
    FOR EACH ROW EXECUTE FUNCTION sec.guard_processing_artifact();

-- TRUNCATE does not invoke row triggers; preserve sealed selections there too.
CREATE FUNCTION sec.guard_processing_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE sealed boolean;
BEGIN
    IF current_setting('transaction_isolation') NOT IN ('read committed','read uncommitted') THEN
        RAISE EXCEPTION 'Artifact writes require read committed isolation' USING ERRCODE='0A000';
    END IF;
    SELECT EXISTS (SELECT 1 FROM sec.filing_processing_sets p WHERE
        CASE TG_TABLE_NAME
            WHEN 'financial_facts' THEN p.fact_source_document_id IS NOT NULL
            WHEN 'financial_tables' THEN p.table_source_document_id IS NOT NULL
            WHEN 'financial_table_facts' THEN p.table_source_document_id IS NOT NULL
            WHEN 'table_cells' THEN p.table_source_document_id IS NOT NULL
            WHEN 'cell_fact_links' THEN p.table_source_document_id IS NOT NULL
            WHEN 'chunks' THEN p.chunking_version IS NOT NULL
            WHEN 'reports' THEN p.report_id IS NOT NULL
            WHEN 'report_sections' THEN p.report_id IS NOT NULL
            ELSE true END) INTO sealed;
    IF sealed THEN
        RAISE EXCEPTION 'Cannot truncate artifact scopes preserved by processing sets' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER facts_processing_truncate BEFORE TRUNCATE ON sec.financial_facts
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER tables_processing_truncate BEFORE TRUNCATE ON sec.financial_tables
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER reports_processing_truncate BEFORE TRUNCATE ON sec.reports
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER chunks_processing_truncate BEFORE TRUNCATE ON sec.chunks
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER sections_processing_truncate BEFORE TRUNCATE ON sec.report_sections
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER links_processing_truncate BEFORE TRUNCATE ON sec.financial_table_facts
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER sources_processing_truncate BEFORE TRUNCATE ON sec.source_documents
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();
CREATE TRIGGER sets_processing_truncate BEFORE TRUNCATE ON sec.filing_processing_sets
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_processing_truncate();

CREATE VIEW sec.filing_overview AS
SELECT f.id AS filing_id,c.id AS company_id,c.cik,c.name AS company_name,c.ticker,
    f.accession_number,f.form,f.filing_date,f.reporting_period_end,f.source_url,f.amendment_of_id,
    p.id AS processing_set_id,p.set_key,p.coverage,p.validation_status,p.validation,p.configuration,p.counts,
    p.fact_source_document_id,p.fact_extraction_version,p.table_source_document_id,p.table_extraction_version,
    p.report_id,r.extraction_version AS report_extraction_version,p.chunking_version,choice.selected_at
FROM sec.filings AS f JOIN sec.companies AS c ON c.id=f.company_id
LEFT JOIN sec.preferred_processing_sets AS choice ON choice.filing_id=f.id
LEFT JOIN sec.filing_processing_sets AS p ON p.id=choice.processing_set_id
LEFT JOIN sec.reports AS r ON r.id=p.report_id;

CREATE VIEW sec.section_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.coverage,
    p.coverage->>'reports' AS stage_coverage,
    rs.id AS section_id,rs.filing_id,rs.report_id,rs.section_key,rs.section_type,st.topic AS section_topic,
    rs.original_title,rs.part,rs.item,rs.section_order,rs.offset_start,rs.offset_end,rs.source_locator,
    rs.validation_status AS section_validation_status,
    substring(r.full_text FROM rs.offset_start+1 FOR rs.offset_end-rs.offset_start) AS text,
    r.extraction_version,r.source_document_id,s.source_url,s.local_path,s.sha256,
    f.accession_number,f.form,f.filing_date,f.reporting_period_end,c.id AS company_id,c.cik,c.name AS company_name,c.ticker
FROM sec.preferred_processing_sets AS choice JOIN sec.filing_processing_sets AS p ON p.id=choice.processing_set_id
JOIN sec.reports AS r ON r.id=p.report_id JOIN sec.report_sections AS rs ON rs.report_id=r.id
JOIN sec.section_types AS st ON st.code=rs.section_type JOIN sec.source_documents AS s ON s.id=r.source_document_id
JOIN sec.filings AS f ON f.id=p.filing_id JOIN sec.companies AS c ON c.id=f.company_id;

CREATE VIEW sec.statement_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.coverage,p.coverage->>'tables' AS stage_coverage,
    t.id AS table_id,t.filing_id,t.source_document_id,t.extraction_version,t.table_key,t.statement_type,t.structure,t.readable_text,
    s.source_url,s.local_path,s.sha256,s.role AS source_role,
    f.accession_number,f.form,f.filing_date,f.reporting_period_end,c.id AS company_id,c.cik,c.name AS company_name,c.ticker,
    p.fact_source_document_id,p.fact_extraction_version
FROM sec.preferred_processing_sets AS choice JOIN sec.filing_processing_sets AS p ON p.id=choice.processing_set_id
JOIN sec.financial_tables AS t ON t.source_document_id=p.table_source_document_id AND t.extraction_version=p.table_extraction_version
JOIN sec.source_documents AS s ON s.id=t.source_document_id
JOIN sec.filings AS f ON f.id=p.filing_id JOIN sec.companies AS c ON c.id=f.company_id;

CREATE VIEW sec.fact_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.coverage,p.coverage->>'facts' AS stage_coverage,fp.*
FROM sec.preferred_processing_sets AS choice JOIN sec.filing_processing_sets AS p ON p.id=choice.processing_set_id
JOIN sec.fact_provenance AS fp ON fp.source_document_id=p.fact_source_document_id AND fp.extraction_version=p.fact_extraction_version;

CREATE VIEW sec.chunk_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.coverage,p.coverage->>'chunks' AS stage_coverage,
    cc.*,ch.section_id,rs.section_type,rs.original_title,rs.validation_status AS section_validation_status,
    st.topic AS section_topic
FROM sec.preferred_processing_sets AS choice JOIN sec.filing_processing_sets AS p ON p.id=choice.processing_set_id
JOIN sec.chunk_citations AS cc ON cc.report_id=p.report_id AND cc.chunking_version=p.chunking_version
JOIN sec.chunks AS ch ON ch.id=cc.chunk_id LEFT JOIN sec.report_sections AS rs ON rs.id=ch.section_id
LEFT JOIN sec.section_types AS st ON st.code=rs.section_type;

COMMENT ON TABLE sec.filing_processing_sets IS
    'Immutable explicit artifact selection. Creating a set seals its scopes, even for partial or unvalidated output. Counts are measured, coverage is caller-declared; passed checks do not assert human review.';
COMMENT ON VIEW sec.filing_overview IS
    'One row per filing; selected-set fields are NULL until a passed set is explicitly preferred. Counts describe only that selected set.';
COMMENT ON VIEW sec.fact_catalog IS
    'Preferred set fact occurrences with source provenance. Occurrences can duplicate a metric; this view is not safe to sum without concept, period, unit and dimension selection.';
