CREATE SCHEMA IF NOT EXISTS sec;

CREATE TABLE sec.companies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cik text NOT NULL UNIQUE CHECK (cik ~ '^[0-9]{10}$'),
    name text NOT NULL CHECK (btrim(name) <> ''),
    ticker text CHECK (ticker IS NULL OR btrim(ticker) <> ''),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sec.filings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES sec.companies(id),
    accession_number text NOT NULL UNIQUE
        CHECK (accession_number ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$'),
    form text NOT NULL CHECK (btrim(form) <> ''),
    filing_date date NOT NULL,
    reporting_period_end date,
    source_url text CHECK (source_url IS NULL OR btrim(source_url) <> ''),
    amendment_of_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, company_id),
    FOREIGN KEY (amendment_of_id, company_id)
        REFERENCES sec.filings(id, company_id),
    CHECK (amendment_of_id IS NULL OR amendment_of_id <> id),
    CHECK (reporting_period_end IS NULL OR reporting_period_end <= filing_date)
);

CREATE TABLE sec.source_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    role text NOT NULL CHECK (btrim(role) <> ''),
    source_url text NOT NULL CHECK (btrim(source_url) <> ''),
    local_path text NOT NULL CHECK (btrim(local_path) <> ''),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    retrieved_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (filing_id, local_path),
    UNIQUE (id, filing_id)
);

CREATE TABLE sec.reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    source_document_id uuid NOT NULL,
    extraction_version text NOT NULL CHECK (btrim(extraction_version) <> ''),
    full_text text NOT NULL,
    sections jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(sections) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_document_id, extraction_version),
    UNIQUE (id, filing_id),
    FOREIGN KEY (source_document_id, filing_id)
        REFERENCES sec.source_documents(id, filing_id)
);

CREATE TABLE sec.chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    report_id uuid NOT NULL,
    chunking_version text NOT NULL CHECK (btrim(chunking_version) <> ''),
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    section text,
    text text NOT NULL CHECK (char_length(text) > 0),
    offset_start integer NOT NULL CHECK (offset_start >= 0),
    offset_end integer NOT NULL CHECK (offset_end > offset_start),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id, chunking_version, chunk_index),
    FOREIGN KEY (report_id, filing_id) REFERENCES sec.reports(id, filing_id)
);

CREATE TABLE sec.financial_facts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    source_document_id uuid NOT NULL,
    extraction_version text NOT NULL CHECK (btrim(extraction_version) <> ''),
    occurrence_key text NOT NULL CHECK (btrim(occurrence_key) <> ''),
    concept_namespace text NOT NULL CHECK (btrim(concept_namespace) <> ''),
    concept_name text NOT NULL CHECK (btrim(concept_name) <> ''),
    label text,
    numeric_value numeric,
    raw_value text,
    is_nil boolean NOT NULL DEFAULT false,
    reported_decimals text
        CHECK (reported_decimals IS NULL OR reported_decimals = 'INF'
               OR reported_decimals ~ '^[+-]?[0-9]+$'),
    reported_precision text
        CHECK (reported_precision IS NULL OR reported_precision = 'INF'
               OR reported_precision ~ '^([+]?[0-9]+|-0+)$'),
    unit jsonb CHECK (unit IS NULL OR jsonb_typeof(unit) = 'object'),
    period_kind text NOT NULL CHECK (period_kind IN ('duration', 'instant', 'forever')),
    period_start date,
    period_end date,
    instant_date date,
    dimensions jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(dimensions) = 'array'),
    source_reference jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(source_reference) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_document_id, extraction_version, occurrence_key),
    UNIQUE (id, filing_id),
    FOREIGN KEY (source_document_id, filing_id)
        REFERENCES sec.source_documents(id, filing_id),
    CHECK (numeric_value IS NULL OR
           numeric_value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    CHECK (NOT is_nil OR numeric_value IS NULL),
    CHECK (is_nil OR numeric_value IS NOT NULL OR raw_value IS NOT NULL),
    CHECK (
        (period_kind = 'duration' AND period_start IS NOT NULL
         AND period_end IS NOT NULL AND period_start <= period_end
         AND instant_date IS NULL)
        OR (period_kind = 'instant' AND instant_date IS NOT NULL
            AND period_start IS NULL AND period_end IS NULL)
        OR (period_kind = 'forever' AND period_start IS NULL
            AND period_end IS NULL AND instant_date IS NULL)
    )
);

CREATE TABLE sec.financial_tables (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    source_document_id uuid NOT NULL,
    extraction_version text NOT NULL CHECK (btrim(extraction_version) <> ''),
    table_key text NOT NULL CHECK (btrim(table_key) <> ''),
    statement_type text NOT NULL CHECK (btrim(statement_type) <> ''),
    structure jsonb NOT NULL CHECK (jsonb_typeof(structure) = 'object'),
    readable_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_document_id, extraction_version, table_key),
    UNIQUE (id, filing_id),
    FOREIGN KEY (source_document_id, filing_id)
        REFERENCES sec.source_documents(id, filing_id)
);

CREATE TABLE sec.financial_table_facts (
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    table_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_id, fact_id),
    FOREIGN KEY (table_id, filing_id) REFERENCES sec.financial_tables(id, filing_id),
    FOREIGN KEY (fact_id, filing_id) REFERENCES sec.financial_facts(id, filing_id)
);

CREATE INDEX filings_company_date_idx ON sec.filings(company_id, filing_date DESC);
CREATE INDEX filings_form_date_idx ON sec.filings(form, filing_date DESC);
CREATE INDEX filings_amendment_idx ON sec.filings(amendment_of_id, company_id)
    WHERE amendment_of_id IS NOT NULL;
CREATE INDEX reports_filing_idx ON sec.reports(filing_id);
CREATE INDEX chunks_filing_idx ON sec.chunks(filing_id);
CREATE INDEX financial_facts_filing_idx ON sec.financial_facts(filing_id);
CREATE INDEX financial_facts_concept_period_idx
    ON sec.financial_facts(concept_namespace, concept_name, period_end, instant_date);
CREATE INDEX financial_tables_filing_idx ON sec.financial_tables(filing_id);
CREATE INDEX financial_table_facts_fact_idx ON sec.financial_table_facts(fact_id, filing_id);
CREATE INDEX financial_table_facts_filing_idx ON sec.financial_table_facts(filing_id);

CREATE FUNCTION sec.validate_chunk_offsets() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    report_text text;
BEGIN
    -- A share lock prevents a concurrent report edit from invalidating this check.
    SELECT full_text INTO report_text
    FROM sec.reports
    WHERE id = NEW.report_id AND filing_id = NEW.filing_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Chunk report does not exist in the specified filing'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.offset_start IS NULL OR NEW.offset_end IS NULL
       OR NEW.offset_start < 0 OR NEW.offset_end <= NEW.offset_start
       OR NEW.offset_end > char_length(report_text) THEN
        RAISE EXCEPTION 'Chunk offsets must identify a nonempty range within the report'
            USING ERRCODE = '23514';
    END IF;
    IF substring(report_text FROM NEW.offset_start + 1
                 FOR NEW.offset_end - NEW.offset_start) IS DISTINCT FROM NEW.text THEN
        RAISE EXCEPTION 'Chunk text does not match its report character range'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER chunks_validate_offsets
BEFORE INSERT OR UPDATE OF report_id, filing_id, text, offset_start, offset_end
ON sec.chunks FOR EACH ROW EXECUTE FUNCTION sec.validate_chunk_offsets();

CREATE FUNCTION sec.validate_report_chunk_ranges() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.full_text IS DISTINCT FROM OLD.full_text AND EXISTS (
        SELECT 1 FROM sec.chunks AS c
        WHERE c.report_id = OLD.id
          AND (c.offset_end > char_length(NEW.full_text)
               OR substring(NEW.full_text FROM c.offset_start + 1
                            FOR c.offset_end - c.offset_start) IS DISTINCT FROM c.text)
    ) THEN
        RAISE EXCEPTION 'Report edit would invalidate existing chunks; create a new extraction version'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER reports_validate_chunk_ranges
BEFORE UPDATE OF full_text ON sec.reports
FOR EACH ROW EXECUTE FUNCTION sec.validate_report_chunk_ranges();

CREATE VIEW sec.filing_catalog AS
SELECT
    f.id AS filing_id,
    c.id AS company_id,
    c.cik,
    c.name AS company_name,
    c.ticker,
    f.accession_number,
    f.form,
    f.filing_date,
    f.reporting_period_end,
    f.source_url,
    f.amendment_of_id,
    f.created_at,
    (SELECT count(*) FROM sec.source_documents AS s WHERE s.filing_id = f.id)
        AS source_document_count,
    (SELECT count(*) FROM sec.reports AS r WHERE r.filing_id = f.id) AS report_count,
    (SELECT count(*) FROM sec.chunks AS ch WHERE ch.filing_id = f.id) AS chunk_count,
    (SELECT count(*) FROM sec.financial_facts AS ff WHERE ff.filing_id = f.id) AS fact_count,
    (SELECT count(*) FROM sec.financial_tables AS ft WHERE ft.filing_id = f.id)
        AS financial_table_count
FROM sec.filings AS f
JOIN sec.companies AS c ON c.id = f.company_id;

CREATE VIEW sec.chunk_citations AS
SELECT
    ch.id AS chunk_id,
    ch.filing_id,
    ch.report_id,
    ch.chunking_version,
    ch.chunk_index,
    ch.section,
    ch.text,
    ch.offset_start,
    ch.offset_end,
    ch.metadata,
    r.extraction_version,
    s.id AS source_document_id,
    s.role AS source_role,
    s.source_url,
    s.local_path,
    s.sha256,
    s.retrieved_at,
    f.accession_number,
    f.form,
    f.filing_date,
    f.reporting_period_end,
    f.source_url AS filing_source_url,
    c.id AS company_id,
    c.cik,
    c.name AS company_name,
    c.ticker
FROM sec.chunks AS ch
JOIN sec.reports AS r ON (r.id, r.filing_id) = (ch.report_id, ch.filing_id)
JOIN sec.source_documents AS s ON (s.id, s.filing_id) = (r.source_document_id, r.filing_id)
JOIN sec.filings AS f ON f.id = ch.filing_id
JOIN sec.companies AS c ON c.id = f.company_id;

CREATE VIEW sec.fact_provenance AS
SELECT
    ff.id AS fact_id,
    ff.filing_id,
    ff.source_document_id,
    ff.extraction_version,
    ff.occurrence_key,
    ff.concept_namespace,
    ff.concept_name,
    ff.label,
    ff.numeric_value,
    ff.raw_value,
    ff.is_nil,
    ff.reported_decimals,
    ff.reported_precision,
    ff.unit,
    ff.period_kind,
    ff.period_start,
    ff.period_end,
    ff.instant_date,
    ff.dimensions,
    ff.source_reference,
    ff.created_at,
    s.role AS source_role,
    s.source_url,
    s.local_path,
    s.sha256,
    s.retrieved_at,
    f.accession_number,
    f.form,
    f.filing_date,
    f.reporting_period_end,
    f.source_url AS filing_source_url,
    c.id AS company_id,
    c.cik,
    c.name AS company_name,
    c.ticker
FROM sec.financial_facts AS ff
JOIN sec.source_documents AS s
    ON (s.id, s.filing_id) = (ff.source_document_id, ff.filing_id)
JOIN sec.filings AS f ON f.id = ff.filing_id
JOIN sec.companies AS c ON c.id = f.company_id;

COMMENT ON TABLE sec.source_documents IS
    'References immutable source bytes; local_path identifies a source within a filing.';
COMMENT ON COLUMN sec.reports.sections IS
    'Extractor-owned array of section descriptors; offsets, when present, use Unicode characters.';
COMMENT ON COLUMN sec.chunks.offset_start IS
    'Inclusive zero-based Unicode character offset into the exact referenced report full_text.';
COMMENT ON COLUMN sec.chunks.offset_end IS
    'Exclusive zero-based Unicode character offset into the exact referenced report full_text.';
COMMENT ON COLUMN sec.financial_facts.occurrence_key IS
    'Extractor-stable identity for an individual occurrence, not just its concept or context.';
COMMENT ON COLUMN sec.financial_facts.numeric_value IS
    'Exact finite reported value; use Decimal in Python. NULL is distinct from zero.';
COMMENT ON COLUMN sec.financial_facts.unit IS
    'Unit object preserving namespace-qualified numerator and denominator measures.';
COMMENT ON COLUMN sec.financial_facts.dimensions IS
    'Dimension descriptors retaining explicit member QNames or typed values and namespaces.';
COMMENT ON VIEW sec.filing_catalog IS
    'One row per filing with metadata and record counts; no financial-value aggregation.';
COMMENT ON VIEW sec.chunk_citations IS
    'One row per chunk, including report version, character range, filing and source provenance.';
COMMENT ON VIEW sec.fact_provenance IS
    'One row per fact occurrence with original unit, period, dimensions and source provenance.';
