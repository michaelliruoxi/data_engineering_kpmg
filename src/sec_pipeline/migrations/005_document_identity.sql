-- Source URLs identify documents within a filing; filesystem paths are locators.
-- Refuse ambiguous historical identities rather than merging evidence silently.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM sec.source_documents GROUP BY filing_id, source_url HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Duplicate filing/source URLs require explicit reconciliation before migration';
    END IF;
END $$;

ALTER TABLE sec.source_documents ADD CONSTRAINT source_documents_filing_url_key
    UNIQUE (filing_id, source_url);
ALTER TABLE sec.source_documents ADD COLUMN title text;
ALTER TABLE sec.source_documents ADD COLUMN document_type text;
ALTER TABLE sec.source_documents ADD COLUMN storage_uri text;
ALTER TABLE sec.filings ADD COLUMN company_name_as_filed text
    CHECK (company_name_as_filed IS NULL OR btrim(company_name_as_filed) <> '');

COMMENT ON COLUMN sec.source_documents.local_path IS
    'First registered repository-relative locator; not the document identity or a downloadable attachment.';
COMMENT ON COLUMN sec.source_documents.retrieved_at IS
    'Timestamp of the first registered retrieval; re-verifying identical bytes preserves this evidence.';
COMMENT ON COLUMN sec.source_documents.storage_uri IS
    'Optional shared storage location; original SEC URL and content hash remain provenance.';
COMMENT ON COLUMN sec.filings.company_name_as_filed IS
    'Name supplied by the source manifest for this filing; NULL for historical records without a captured snapshot.';
