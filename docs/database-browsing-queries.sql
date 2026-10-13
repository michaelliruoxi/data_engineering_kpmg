-- Read-only examples, migrations 001–005. Preferred catalogs are empty until
-- a passed processing set is explicitly selected. Add accession filters as needed.

-- 1. See chosen versions and scope; NULL set_key means no preferred selection.
SELECT company_name, cik, form, accession_number, filing_date, set_key,
       coverage, validation_status, counts,
       fact_extraction_version, table_extraction_version,
       report_extraction_version, chunking_version
FROM sec.filing_overview ORDER BY filing_date DESC, accession_number;

-- 2. Read MD&A across annual and quarterly forms without item-number guessing.
SELECT company_name, accession_number, part, item, original_title,
       section_validation_status, stage_coverage, text, source_url
FROM sec.section_catalog WHERE section_type = 'mda'
ORDER BY filing_date DESC, accession_number, section_order;

-- 3. Browse all governance and risk sections, including controls and market risk.
SELECT accession_number, section_topic, section_type, original_title,
       section_validation_status, stage_coverage, text, source_locator
FROM sec.section_catalog WHERE section_topic IN ('governance', 'risks')
ORDER BY accession_number, section_order;

-- 4. Inspect one row per physical statement cell, with exact fact references.
SELECT accession_number, table_key, row_index, column_index, row_heading,
       column_headings, display_text, resolution_status, fact_count, linked_facts
FROM sec.statement_cells
ORDER BY accession_number, table_key, row_index, column_index;

-- 5. Retrieve exact narrative citations for MD&A and risk factors.
SELECT accession_number, section_type, text, offset_start, offset_end, source_url
FROM sec.chunk_catalog WHERE section_type IN ('mda', 'risk_factors')
ORDER BY accession_number, chunk_index;

-- 6. Compare available report versions before choosing one; no preference required.
SELECT f.accession_number, r.id AS report_id, r.extraction_version,
       count(s.id) AS section_count, char_length(r.full_text) AS characters
FROM sec.filings f JOIN sec.reports r ON r.filing_id = f.id
LEFT JOIN sec.report_sections s ON s.report_id = r.id
GROUP BY f.accession_number, r.id
ORDER BY f.accession_number, r.extraction_version;

-- 7. Inspect exact fact occurrences; select period, unit and dimensions deliberately.
SELECT accession_number, concept_name, numeric_value, is_nil,
       period_kind, period_start, period_end, instant_date, unit, dimensions,
       source_url, extraction_version, stage_coverage
FROM sec.fact_catalog ORDER BY accession_number, concept_name, occurrence_key;

-- 8. Preflight before upgrading an existing 001-only database.
-- Any row needs explicit reconciliation before migration 005 can proceed.
SELECT filing_id, source_url, count(*) AS duplicate_documents
FROM sec.source_documents GROUP BY filing_id, source_url HAVING count(*) > 1;
