-- Explicit legacy catalogs retain their preferred legacy selection even when
-- finance readers serve a separately published corrected source set.
CREATE OR REPLACE VIEW sec.chunk_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.coverage,p.coverage->>'chunks' AS stage_coverage,
    cc.*,ch.section_id,rs.section_type,rs.original_title,rs.validation_status AS section_validation_status,
    st.topic AS section_topic
FROM sec.preferred_processing_sets AS choice JOIN sec.filing_processing_sets AS p ON p.id=choice.processing_set_id
JOIN sec.chunk_citations_history AS cc ON cc.report_id=p.report_id AND cc.chunking_version=p.chunking_version
JOIN sec.chunks AS ch ON ch.id=cc.chunk_id LEFT JOIN sec.report_sections AS rs ON rs.id=ch.section_id
LEFT JOIN sec.section_types AS st ON st.code=rs.section_type;

COMMENT ON VIEW sec.chunk_catalog IS
 'Explicit preferred legacy set chunks, consistent with section_catalog, statement_catalog and fact_catalog. Corrected source publication does not change this legacy preference. Use finance.filing_chunk for the current served selection.';
COMMENT ON VIEW sec.analysis_ready_facts IS
 'Passed facts for the single served source processing set. Returns no source rows while the served family is legacy, including for passed V4 sets. Use source_fact_catalog with an explicit processing_set_id for historical auditing.';
COMMENT ON VIEW sec.analysis_ready_statement_cells IS
 'Passed cells for the single served source processing set. Returns no source rows while the served family is legacy, including for passed V4 sets. Use source_statement_cells with an explicit processing_set_id for historical auditing.';
