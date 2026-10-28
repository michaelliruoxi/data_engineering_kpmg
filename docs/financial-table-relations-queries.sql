-- Read-only acceptance against the latest schema (001–009). Requires psql.
-- Supply actual receiving IDs; do not copy UUIDs from local test evidence.
-- psql ... -v accession=0001193125-26-191507 -v contract=legacy
--          -v processing_set_id=ACTUAL_SHARED_UUID -f this_file.sql
-- No newest-version inference and no DDL or persistent writes.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '20s';

SELECT current_user,current_database(),current_setting('transaction_read_only') AS read_only;
SELECT ssl,version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid();
SELECT name,sha256 FROM sec.schema_migrations ORDER BY name;

-- Candidate/official preference evidence; timestamps do not imply selection.
SELECT p.* FROM sec.filing_processing_sets p JOIN sec.filings f ON f.id=p.filing_id
WHERE f.accession_number=:'accession' ORDER BY p.set_key;
SELECT p.* FROM sec.preferred_processing_sets p JOIN sec.filings f ON f.id=p.filing_id
WHERE f.accession_number=:'accession';
SELECT p.*,d.xml_document_id,d.primary_html_document_id FROM sec.source_processing_sets p
JOIN sec.source_document_sets d ON d.id=p.document_set_id JOIN sec.filings f ON f.id=p.filing_id
WHERE f.accession_number=:'accession' ORDER BY p.set_key;

-- Table/cell cardinality retains non-fact and multi-fact cells, without join inflation.
WITH chosen AS (
 SELECT t.* FROM sec.financial_tables t JOIN sec.filings f ON f.id=t.filing_id
 JOIN sec.filing_processing_sets p ON p.filing_id=t.filing_id AND p.table_source_document_id=t.source_document_id
   AND p.table_extraction_version=t.extraction_version
 WHERE :'contract'='legacy' AND p.id=:'processing_set_id'::uuid AND f.accession_number=:'accession'
 UNION ALL
 SELECT t.* FROM sec.financial_tables t JOIN sec.source_processing_tables m ON m.table_id=t.id
 JOIN sec.source_processing_sets p ON p.id=m.processing_set_id JOIN sec.filings f ON f.id=p.filing_id
 WHERE :'contract'='source-v4' AND p.id=:'processing_set_id'::uuid AND f.accession_number=:'accession'
)
SELECT t.id,t.table_key,t.extraction_version,t.structure->>'source_xpath' AS original_table_xpath,
 (SELECT count(*) FROM sec.table_cells c WHERE c.table_id=t.id) AS anchor_cells,
 (SELECT count(*) FROM sec.cell_fact_links l WHERE l.table_id=t.id) AS accepted_references,
 (SELECT count(*) FROM sec.financial_table_facts m WHERE m.table_id=t.id) AS unique_memberships
FROM chosen t ORDER BY t.table_key;

-- All broken parent/scope records for this accession, including dangling IDs.
SELECT l.cell_id,l.table_id,l.fact_id,l.filing_id,c.table_id AS cell_table_id,
 f.source_document_id,f.extraction_version,t.structure->'fact_scope' AS declared_scope
FROM sec.cell_fact_links l
LEFT JOIN sec.table_cells c ON c.id=l.cell_id
LEFT JOIN sec.financial_tables t ON t.id=l.table_id
LEFT JOIN sec.financial_facts f ON f.id=l.fact_id
WHERE EXISTS(SELECT 1 FROM sec.filings a WHERE a.accession_number=:'accession'
 AND a.id IN (l.filing_id,c.filing_id,t.filing_id,f.filing_id))
AND (c.id IS NULL OR t.id IS NULL OR f.id IS NULL OR c.table_id<>l.table_id
 OR c.filing_id<>l.filing_id OR t.filing_id<>l.filing_id OR f.filing_id<>l.filing_id
 OR f.source_document_id::text IS DISTINCT FROM t.structure#>>'{fact_scope,xml_document_id}'
 OR f.extraction_version IS DISTINCT FROM t.structure#>>'{fact_scope,facts_version}');

-- Source-only/review records are not accepted links or invented zero/nil facts.
SELECT t.id,t.table_key,c.cell_key,c.resolution_status,c.source_locator,
 o.source_document_id,o.evidence AS source_observation,b.fact_id AS review_fact_id,
 b.occurrence_id,b.document_set_id,q.issue_key,q.code
FROM sec.table_cells c JOIN sec.financial_tables t ON t.id=c.table_id
JOIN sec.filings f ON f.id=t.filing_id
LEFT JOIN sec.cell_source_observations o ON o.cell_id=c.id
LEFT JOIN sec.cell_source_fact_bindings b ON b.cell_id=c.id
LEFT JOIN sec.source_quality_issues q ON q.binding_id=b.id
WHERE f.accession_number=:'accession' AND c.resolution_status IN ('source_only','review_required')
ORDER BY t.id,c.row_index,c.column_index,q.issue_key;

-- Section persistence is optional and NOT deployed by this script.
SELECT to_regclass('sec.financial_table_sections') AS proposed_relation;
-- Only after reviewed deployment, inspect existing associations using:
-- SELECT t.id,t.table_key,c.cell_key,f.id AS fact_id,f.occurrence_key,f.concept_name,
--   s.id AS section_id,s.section_key,s.original_title,s.source_locator,t.structure->>'source_xpath'
-- FROM sec.financial_table_sections m JOIN sec.financial_tables t ON t.id=m.table_id
-- JOIN sec.report_sections s ON s.id=m.section_id JOIN sec.table_cells c ON c.table_id=t.id
-- JOIN sec.cell_fact_links l ON l.cell_id=c.id JOIN sec.financial_facts f ON f.id=l.fact_id
-- WHERE m.table_id=ACTUAL_RECEIVING_TABLE_UUID;
ROLLBACK;
