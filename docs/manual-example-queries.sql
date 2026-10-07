-- Manual example: Microsoft 10-Q, accession 0001193125-26-191507.
-- Select and run any one numbered query independently. All queries are read-only.
-- Extraction and chunking versions are both manual-example-v1.
-- Use the shared RDS connection. The local metadata seed does not load this sample.
-- Expected results below describe the verified October 6, 2026 sample.

-- 1. Sample overview: counts below are version-scoped.
-- The catalog's built-in report/fact/chunk counts include all versions, so this
-- query calculates separate sample counts rather than using those totals.
-- Expect 1 row: sources=4, sample reports=1, chunks=6, facts=24, tables=3, links=24.
-- source_document_count covers the filing's source files across all versions.
SELECT catalog.company_name,
       catalog.accession_number,
       catalog.form,
       catalog.filing_date,
       catalog.reporting_period_end,
       catalog.source_url AS filing_source_url,
       catalog.source_document_count,
       'manual-example-v1' AS sample_version,
       (SELECT count(*)
        FROM sec.reports AS r
        WHERE r.filing_id = catalog.filing_id
          AND r.extraction_version = 'manual-example-v1') AS sample_report_count,
       (SELECT count(*)
        FROM sec.chunks AS ch
        JOIN sec.reports AS r
          ON r.id = ch.report_id AND r.filing_id = ch.filing_id
        WHERE ch.filing_id = catalog.filing_id
          AND r.extraction_version = 'manual-example-v1'
          AND ch.chunking_version = 'manual-example-v1') AS sample_chunk_count,
       (SELECT count(*)
        FROM sec.financial_facts AS ff
        WHERE ff.filing_id = catalog.filing_id
          AND ff.extraction_version = 'manual-example-v1') AS sample_fact_count,
       (SELECT count(*)
        FROM sec.financial_tables AS ft
        WHERE ft.filing_id = catalog.filing_id
          AND ft.extraction_version = 'manual-example-v1') AS sample_table_count,
       (SELECT count(*)
        FROM sec.financial_table_facts AS link
        JOIN sec.financial_tables AS ft
          ON ft.id = link.table_id AND ft.filing_id = link.filing_id
        JOIN sec.financial_facts AS ff
          ON ff.id = link.fact_id AND ff.filing_id = link.filing_id
        WHERE link.filing_id = catalog.filing_id
          AND ft.extraction_version = 'manual-example-v1'
          AND ff.extraction_version = 'manual-example-v1') AS sample_table_fact_link_count
FROM sec.filing_catalog AS catalog
WHERE catalog.accession_number = '0001193125-26-191507';

-- 2. Financial facts: numeric_value is in the stored unit, not display millions.
-- Only plain USD facts receive a value_usd_millions conversion. Other units
-- remain NULL in that display column. Periods and dimensions stay visible.
-- Expect 24 rows. January-March 2026 total revenue is 82,886,000,000 USD,
-- or 82,886 in value_usd_millions. The prior-year revenue is a separate fact.
SELECT fact_id,
       concept_namespace,
       concept_name,
       label,
       numeric_value,
       CASE WHEN unit->'numerator' = '["{http://www.xbrl.org/2003/iso4217}USD"]'::jsonb
                  AND unit->'denominator' = '[]'::jsonb
            THEN numeric_value / 1000000::numeric
            ELSE NULL
       END AS value_usd_millions,
       raw_value,
       is_nil,
       reported_decimals,
       reported_precision,
       unit,
       period_kind,
       period_start,
       period_end,
       instant_date,
       dimensions,
       extraction_version,
       occurrence_key,
       source_reference,
       source_document_id,
       source_url,
       local_path,
       sha256
FROM sec.fact_provenance
WHERE accession_number = '0001193125-26-191507'
  AND extraction_version = 'manual-example-v1'
ORDER BY concept_namespace, concept_name, period_start, period_end,
         instant_date, occurrence_key;

-- 3. Statement excerpts: readable_text is convenient for browsing;
-- structure preserves this manual example's cell and source conventions.
-- Expect 3 rows: income statement, balance sheet, and cash-flow excerpts.
SELECT ft.id AS table_id,
       ft.table_key,
       ft.statement_type,
       ft.extraction_version,
       ft.readable_text,
       ft.structure,
       ft.source_document_id,
       sd.role AS source_role,
       sd.source_url,
       sd.local_path,
       sd.sha256
FROM sec.financial_tables AS ft
JOIN sec.filings AS f ON f.id = ft.filing_id
JOIN sec.source_documents AS sd
  ON sd.id = ft.source_document_id AND sd.filing_id = ft.filing_id
WHERE f.accession_number = '0001193125-26-191507'
  AND ft.extraction_version = 'manual-example-v1'
ORDER BY ft.statement_type, ft.table_key;

-- 4. Table-to-fact links: both sides are limited to the manual example version.
-- These links express table membership. Consult structure in query 3 for cells.
-- Expect 24 rows. Match table_id to query 3 and fact_id to query 2.
SELECT ft.id AS table_id,
       ft.table_key,
       ft.statement_type,
       ft.extraction_version AS table_version,
       fact.fact_id,
       fact.extraction_version AS fact_version,
       fact.concept_name,
       fact.label,
       fact.numeric_value,
       fact.unit,
       fact.period_kind,
       fact.period_start,
       fact.period_end,
       fact.instant_date,
       fact.dimensions,
       fact.occurrence_key,
       fact.source_reference,
       fact.source_url AS fact_source_url,
       fact.sha256 AS fact_source_sha256
FROM sec.financial_table_facts AS link
JOIN sec.financial_tables AS ft
  ON ft.id = link.table_id AND ft.filing_id = link.filing_id
JOIN sec.fact_provenance AS fact
  ON fact.fact_id = link.fact_id AND fact.filing_id = link.filing_id
WHERE fact.accession_number = '0001193125-26-191507'
  AND ft.extraction_version = 'manual-example-v1'
  AND fact.extraction_version = 'manual-example-v1'
ORDER BY ft.table_key, fact.concept_name, fact.period_start,
         fact.period_end, fact.instant_date, fact.occurrence_key;

-- 5. Report excerpt: full_text contains selected passages, not the full filing.
-- Expect 1 row with section_count = 2. Sections are stored together in JSON.
SELECT r.id AS report_id,
       r.extraction_version,
       jsonb_array_length(r.sections) AS section_count,
       r.sections,
       r.full_text,
       r.source_document_id,
       sd.role AS source_role,
       sd.source_url,
       sd.local_path,
       sd.sha256
FROM sec.reports AS r
JOIN sec.filings AS f ON f.id = r.filing_id
JOIN sec.source_documents AS sd
  ON sd.id = r.source_document_id AND sd.filing_id = r.filing_id
WHERE f.accession_number = '0001193125-26-191507'
  AND r.extraction_version = 'manual-example-v1'
ORDER BY r.id;

-- 6. Chunks and citations: offsets are zero-based Unicode character positions.
-- PostgreSQL substring starts at 1, so add 1 to the stored offset_start.
-- Expect 6 rows with text_matches_report = true. Match report_id to query 5.
-- This checks the stored text slice, not extraction accuracy against the HTML.
SELECT citation.chunk_id,
       citation.report_id,
       citation.chunk_index,
       citation.section,
       citation.text,
       citation.offset_start,
       citation.offset_end,
       citation.text = substring(r.full_text
           FROM citation.offset_start + 1
           FOR citation.offset_end - citation.offset_start) AS text_matches_report,
       citation.extraction_version,
       citation.chunking_version,
       citation.metadata,
       citation.company_name,
       citation.accession_number,
       citation.source_document_id,
       citation.source_role,
       citation.source_url,
       citation.local_path,
       citation.sha256
FROM sec.chunk_citations AS citation
JOIN sec.reports AS r
  ON r.id = citation.report_id AND r.filing_id = citation.filing_id
WHERE citation.accession_number = '0001193125-26-191507'
  AND citation.extraction_version = 'manual-example-v1'
  AND citation.chunking_version = 'manual-example-v1'
ORDER BY citation.report_id, citation.chunk_index;
