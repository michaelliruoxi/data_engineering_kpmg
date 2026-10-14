-- Preserve immutable source receipts when registration replays a sealed URL.
-- Existing update/delete protections and every other artifact guard remain unchanged.
CREATE OR REPLACE FUNCTION sec.guard_processing_artifact() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_data jsonb; new_data jsonb; target uuid; is_sealed boolean := false;
    replay boolean; identity_predicate text;
BEGIN
    IF TG_OP <> 'INSERT' THEN old_data := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN new_data := to_jsonb(NEW); END IF;
    FOR target IN SELECT DISTINCT value FROM (
        VALUES ((old_data->>'filing_id')::uuid), ((new_data->>'filing_id')::uuid)
    ) AS ids(value) WHERE value IS NOT NULL ORDER BY value LOOP
        PERFORM sec.lock_processing_filing(target);
    END LOOP;
    IF TG_OP <> 'INSERT' THEN
        is_sealed := sec.processing_artifact_is_sealed(TG_TABLE_NAME,old_data);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        is_sealed := is_sealed OR sec.processing_artifact_is_sealed(TG_TABLE_NAME,new_data);
    END IF;
    IF NOT is_sealed THEN
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    IF TG_OP='UPDATE' AND old_data IS NOT DISTINCT FROM new_data THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME='source_documents' AND TG_OP='UPDATE'
       AND old_data - ARRAY['local_path','retrieved_at','storage_uri','title','document_type'] = new_data - ARRAY['local_path','retrieved_at','storage_uri','title','document_type'] THEN
        RETURN NEW;
    END IF;
    IF TG_OP='INSERT' THEN
        IF TG_TABLE_NAME='source_documents' THEN
            -- Registration conflicts on filing/URL, not the new default UUID.
            -- An INSERT replay retains the first receipt through DO NOTHING.
            SELECT EXISTS (SELECT 1 FROM sec.source_documents x
                WHERE x.filing_id=(new_data->>'filing_id')::uuid
                  AND x.source_url=new_data->>'source_url'
                  AND x.role=new_data->>'role'
                  AND x.sha256=new_data->>'sha256'
                  AND x.size_bytes=(new_data->>'size_bytes')::bigint) INTO replay;
            IF replay THEN RETURN NEW; END IF;
        END IF;
        identity_predicate := CASE TG_TABLE_NAME
            WHEN 'financial_facts' THEN 'x.source_document_id=($1->>''source_document_id'')::uuid AND x.extraction_version=$1->>''extraction_version'' AND x.occurrence_key=$1->>''occurrence_key'''
            WHEN 'financial_tables' THEN 'x.source_document_id=($1->>''source_document_id'')::uuid AND x.extraction_version=$1->>''extraction_version'' AND x.table_key=$1->>''table_key'''
            WHEN 'reports' THEN 'x.source_document_id=($1->>''source_document_id'')::uuid AND x.extraction_version=$1->>''extraction_version'''
            WHEN 'report_sections' THEN 'x.report_id=($1->>''report_id'')::uuid AND x.section_key=$1->>''section_key'''
            WHEN 'chunks' THEN 'x.report_id=($1->>''report_id'')::uuid AND x.chunking_version=$1->>''chunking_version'' AND x.chunk_index=($1->>''chunk_index'')::integer'
            WHEN 'financial_table_facts' THEN 'x.table_id=($1->>''table_id'')::uuid AND x.fact_id=($1->>''fact_id'')::uuid'
            ELSE 'x.id=($1->>''id'')::uuid' END;
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM sec.%I AS x WHERE %s AND to_jsonb(x) - ARRAY[''id'',''created_at'',''relational_cells_version''] = $1 - ARRAY[''id'',''created_at'',''relational_cells_version''])',
                       TG_TABLE_NAME, identity_predicate) INTO replay USING new_data;
        IF replay THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION 'Artifact scope is sealed by a processing set; use a new extraction/chunking version'
        USING ERRCODE = '23514';
END;
$$;
