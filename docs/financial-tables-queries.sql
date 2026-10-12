-- Read-only review; yields no production rows until the reviewed import runs.
SELECT t.table_key, t.extraction_version, t.readable_text, d.source_url, d.sha256
FROM sec.financial_tables t
JOIN sec.filings f ON f.id = t.filing_id
JOIN sec.source_documents d ON d.id = t.source_document_id
WHERE f.accession_number = '0001193125-26-191507'
  AND t.extraction_version = 'tables-v1'
ORDER BY t.table_key;

SELECT t.table_key, count(*) AS membership_links
FROM sec.financial_tables t
JOIN sec.filings f ON f.id = t.filing_id
JOIN sec.financial_table_facts tf ON tf.table_id = t.id
JOIN sec.financial_facts ff ON ff.id = tf.fact_id
WHERE f.accession_number = '0001193125-26-191507'
  AND t.extraction_version = 'tables-v1' AND ff.extraction_version = 'facts-v1'
GROUP BY t.table_key ORDER BY t.table_key;
-- Expected: balance_sheet 76, cash_flow 131, income_statement 76.

SELECT t.table_key, cell->>'cell_id' AS cell_id,
       cell->>'row_heading' AS row_heading, cell->>'display_text' AS display_text,
       ref->>'xml_id' AS original_xml_id, ref->>'method' AS link_method,
       ref->'period' AS fact_period, ref->>'raw_value' AS xml_value
FROM sec.financial_tables t
JOIN sec.filings f ON f.id = t.filing_id
CROSS JOIN LATERAL jsonb_array_elements(t.structure->'rows') AS r(row_json)
CROSS JOIN LATERAL jsonb_array_elements(r.row_json) AS c(cell)
CROSS JOIN LATERAL jsonb_array_elements(c.cell->'fact_references') AS v(ref)
WHERE f.accession_number = '0001193125-26-191507'
  AND t.extraction_version = 'tables-v1'
ORDER BY t.table_key, cell->>'cell_id';

-- Exact expected/actual memberships. A correct reviewed load returns zero rows.
WITH expected AS (
    SELECT DISTINCT t.id AS table_id, t.filing_id, ref->>'xml_id' AS occurrence_key
    FROM sec.financial_tables t JOIN sec.filings f ON f.id=t.filing_id
    CROSS JOIN LATERAL jsonb_array_elements(t.structure->'rows') r(row_json)
    CROSS JOIN LATERAL jsonb_array_elements(r.row_json) c(cell)
    CROSS JOIN LATERAL jsonb_array_elements(c.cell->'fact_references') v(ref)
    WHERE f.accession_number='0001193125-26-191507' AND t.extraction_version='tables-v1'
), actual AS (
    SELECT l.table_id,l.filing_id,ff.occurrence_key
    FROM sec.financial_table_facts l JOIN sec.financial_tables t ON t.id=l.table_id
    JOIN sec.financial_facts ff ON ff.id=l.fact_id
    JOIN sec.filings f ON f.id=t.filing_id
    WHERE f.accession_number='0001193125-26-191507' AND t.extraction_version='tables-v1'
)
SELECT 'missing' AS issue,* FROM (SELECT * FROM expected EXCEPT SELECT * FROM actual) missing
UNION ALL
SELECT 'extra' AS issue,* FROM (SELECT * FROM actual EXCEPT SELECT * FROM expected) extra;

-- Provenance/filing/version failures. A correct reviewed load returns zero rows.
SELECT t.table_key,l.table_id,l.fact_id
FROM sec.financial_table_facts l JOIN sec.financial_tables t ON t.id=l.table_id
JOIN sec.financial_facts ff ON ff.id=l.fact_id
JOIN sec.source_documents html ON html.id=t.source_document_id
JOIN sec.source_documents xml ON xml.id=ff.source_document_id
JOIN sec.filings f ON f.id=t.filing_id
WHERE f.accession_number='0001193125-26-191507' AND t.extraction_version='tables-v1'
  AND (l.filing_id<>t.filing_id OR ff.filing_id<>t.filing_id
       OR html.filing_id<>t.filing_id OR xml.filing_id<>t.filing_id
       OR html.role<>'main_inline_xbrl_filing' OR xml.role<>'extracted_xbrl_instance'
       OR ff.extraction_version<>'facts-v1');
