-- DRAFT: Michael must review naming/deployment; NOT a packaged migration.
-- Apply only explicitly to an isolated local test DB during review.
-- Uses existing table/report/section IDs. Does not rewrite sealed artifacts.
CREATE TABLE sec.financial_table_sections (
    table_id uuid NOT NULL,
    report_id uuid NOT NULL,
    section_id uuid NOT NULL,
    filing_id uuid NOT NULL,
    source_document_id uuid NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    mapping_version text NOT NULL CHECK (mapping_version = 'table-section-dom-v1'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_id, report_id),
    FOREIGN KEY (table_id, filing_id) REFERENCES sec.financial_tables(id, filing_id),
    FOREIGN KEY (report_id, filing_id) REFERENCES sec.reports(id, filing_id),
    FOREIGN KEY (section_id, report_id, filing_id) REFERENCES sec.report_sections(id, report_id, filing_id),
    FOREIGN KEY (source_document_id, filing_id) REFERENCES sec.source_documents(id, filing_id)
);

CREATE FUNCTION sec.guard_financial_table_section() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t sec.financial_tables%ROWTYPE; r sec.reports%ROWTYPE; source sec.source_documents%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Table-section mappings are immutable; use new artifact versions' USING ERRCODE='23514';
    END IF;
    SELECT * INTO t FROM sec.financial_tables WHERE id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT * INTO r FROM sec.reports WHERE id=NEW.report_id AND filing_id=NEW.filing_id;
    SELECT * INTO source FROM sec.source_documents WHERE id=NEW.source_document_id AND filing_id=NEW.filing_id;
    IF t.id IS NULL OR r.id IS NULL OR source.id IS NULL
       OR t.source_document_id IS DISTINCT FROM NEW.source_document_id
       OR r.source_document_id IS DISTINCT FROM NEW.source_document_id
       OR source.sha256 IS DISTINCT FROM NEW.source_sha256
       OR t.structure->>'html_sha256' IS DISTINCT FROM NEW.source_sha256
       OR NEW.evidence->>'mapping_status' IS DISTINCT FROM 'mapped'
       OR NEW.evidence->>'table_id' IS DISTINCT FROM NEW.table_id::text
       OR NEW.evidence->>'report_id' IS DISTINCT FROM NEW.report_id::text
       OR NEW.evidence->>'section_uuid' IS DISTINCT FROM NEW.section_id::text
       OR NEW.evidence#>>'{evidence,html_sha256}' IS DISTINCT FROM NEW.source_sha256
       OR NEW.evidence->>'table_xpath' IS DISTINCT FROM t.structure->>'source_xpath' THEN
        RAISE EXCEPTION 'Table-section identity/source evidence mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER financial_table_section_guard BEFORE INSERT OR UPDATE OR DELETE
ON sec.financial_table_sections FOR EACH ROW EXECUTE FUNCTION sec.guard_financial_table_section();

COMMENT ON TABLE sec.financial_table_sections IS
'DRAFT additive DOM-verified table/report-section associations. No new table identity, automatic selection, or shared deployment approval.';
