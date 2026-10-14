-- Explicit, evidence-backed issuer history; filing identities remain unchanged.
CREATE TABLE sec.issuer_successions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    successor_company_id uuid NOT NULL REFERENCES sec.companies(id),
    predecessor_company_id uuid NOT NULL REFERENCES sec.companies(id),
    effective_date date NOT NULL,
    evidence_url text NOT NULL CHECK (
        evidence_url ~ '^https://www[.]sec[.]gov/Archives/edgar/data/[1-9][0-9]{0,9}/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_local_path text NOT NULL CHECK (
        btrim(evidence_local_path) <> ''
        AND evidence_local_path !~ '(^[/\\]|^[A-Za-z]:|(^|[/\\])[.][.]?([/\\]|$)|[[:cntrl:]])'
    ),
    evidence_retrieved_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (successor_company_id, predecessor_company_id),
    CHECK (successor_company_id <> predecessor_company_id)
);

CREATE INDEX issuer_successions_predecessor_idx ON sec.issuer_successions(predecessor_company_id);

CREATE FUNCTION sec.guard_issuer_succession() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    successor_cik text;
    predecessor_cik text;
    archive_cik text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Issuer succession evidence is immutable; preserve the original relationship and receipt'
            USING ERRCODE = '23514';
    END IF;
    SELECT cik INTO successor_cik FROM sec.companies WHERE id=NEW.successor_company_id FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Successor company identity does not exist' USING ERRCODE = '23503';
    END IF;
    SELECT cik INTO predecessor_cik FROM sec.companies WHERE id=NEW.predecessor_company_id FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Predecessor company identity does not exist' USING ERRCODE = '23503';
    END IF;
    archive_cik := substring(NEW.evidence_url FROM '^https://www[.]sec[.]gov/Archives/edgar/data/([1-9][0-9]{0,9})/');
    IF archive_cik IS NULL OR lpad(archive_cik, 10, '0') NOT IN (successor_cik, predecessor_cik) THEN
        RAISE EXCEPTION 'Succession evidence must be filed under one of the linked issuer CIKs'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER issuer_successions_guard
    BEFORE INSERT OR UPDATE OR DELETE ON sec.issuer_successions
    FOR EACH ROW EXECUTE FUNCTION sec.guard_issuer_succession();
CREATE TRIGGER issuer_successions_no_truncate
    BEFORE TRUNCATE ON sec.issuer_successions
    FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_issuer_succession();

CREATE FUNCTION sec.guard_linked_issuer_cik() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.cik IS DISTINCT FROM OLD.cik AND EXISTS (
        SELECT 1 FROM sec.issuer_successions
        WHERE successor_company_id=OLD.id OR predecessor_company_id=OLD.id
    ) THEN
        RAISE EXCEPTION 'A linked issuer CIK is immutable; company profile names may be updated separately'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER companies_preserve_linked_issuer_cik
    BEFORE UPDATE OF cik ON sec.companies
    FOR EACH ROW EXECUTE FUNCTION sec.guard_linked_issuer_cik();

CREATE VIEW sec.issuer_filing_history AS
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
    WHERE selection.validation_status='passed' AND selection.table_extraction_version='tables-v2'
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

COMMENT ON TABLE sec.issuer_successions IS
    'Caller-verified issuer continuity with immutable effective date and source evidence. Links do not rename or reassign filings, facts, reports, tables, or chunks.';
COMMENT ON COLUMN sec.issuer_successions.evidence_retrieved_at IS
    'Instant of first archived evidence receipt, supplied with a timezone and stored as timestamptz; identical replay preserves it.';
COMMENT ON VIEW sec.issuer_filing_history IS
    'One row per current issuer and original filing. Direct ownership takes precedence, then verified tables-v2 index co-registration, then an explicit predecessor link through its effective date. Original filing identities and names are retained; history is not transitive.';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
        GRANT SELECT ON sec.issuer_successions, sec.issuer_filing_history TO sec_reader;
    END IF;
END $$;
