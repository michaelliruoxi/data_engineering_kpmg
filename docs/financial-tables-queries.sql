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
