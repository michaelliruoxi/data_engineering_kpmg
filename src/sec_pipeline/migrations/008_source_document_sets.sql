-- V4 is an additive source/quality contract. Legacy selections stay unchanged.
CREATE TABLE sec.source_document_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filing_id uuid NOT NULL REFERENCES sec.filings(id),
    set_key text NOT NULL CHECK (btrim(set_key)<>''),
    xml_document_id uuid NOT NULL, primary_html_document_id uuid NOT NULL, index_document_id uuid NOT NULL,
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    membership_sha256 text NOT NULL CHECK (membership_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL CHECK (policy_version='inline-set-v1'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(filing_id,set_key), UNIQUE(id,filing_id),
    FOREIGN KEY(xml_document_id,filing_id) REFERENCES sec.source_documents(id,filing_id),
    FOREIGN KEY(primary_html_document_id,filing_id) REFERENCES sec.source_documents(id,filing_id),
    FOREIGN KEY(index_document_id,filing_id) REFERENCES sec.source_documents(id,filing_id),
    CHECK(xml_document_id<>primary_html_document_id AND xml_document_id<>index_document_id AND primary_html_document_id<>index_document_id)
);
CREATE TABLE sec.source_document_set_members (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), document_set_id uuid NOT NULL, filing_id uuid NOT NULL,
    source_document_id uuid NOT NULL, source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
    sequence integer NOT NULL CHECK(sequence>0), document_type text NOT NULL CHECK(btrim(document_type)<>''),
    is_primary boolean NOT NULL, index_locator jsonb NOT NULL CHECK(jsonb_typeof(index_locator)='object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_set_id,source_document_id), UNIQUE(document_set_id,sequence),
    FOREIGN KEY(document_set_id,filing_id) REFERENCES sec.source_document_sets(id,filing_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(source_document_id,filing_id) REFERENCES sec.source_documents(id,filing_id),
    CHECK(upper(document_type) NOT IN ('EX-FILING FEES','EX-98'))
);
CREATE TABLE sec.fact_source_occurrences (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filing_id uuid NOT NULL, document_set_id uuid NOT NULL,
    fact_id uuid NOT NULL, html_document_id uuid NOT NULL, inline_id text NOT NULL CHECK(btrim(inline_id)<>''),
    source_xpath text NOT NULL CHECK(btrim(source_xpath)<>''), evidence jsonb NOT NULL CHECK(jsonb_typeof(evidence)='object'),
    fragments jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(fragments)='array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_set_id,fact_id), UNIQUE(document_set_id,inline_id),
    UNIQUE(document_set_id,html_document_id,source_xpath), UNIQUE(id,document_set_id,fact_id,filing_id),
    FOREIGN KEY(document_set_id,filing_id) REFERENCES sec.source_document_sets(id,filing_id),
    FOREIGN KEY(document_set_id,html_document_id) REFERENCES sec.source_document_set_members(document_set_id,source_document_id),
    FOREIGN KEY(fact_id,filing_id) REFERENCES sec.financial_facts(id,filing_id)
);
CREATE TABLE sec.cell_source_fact_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filing_id uuid NOT NULL, document_set_id uuid NOT NULL,
    table_id uuid NOT NULL, cell_id uuid NOT NULL, fact_id uuid NOT NULL, occurrence_id uuid NOT NULL,
    reference_order integer NOT NULL CHECK(reference_order>=0),
    evidence jsonb NOT NULL CHECK(jsonb_typeof(evidence)='object'), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(cell_id,reference_order), UNIQUE(cell_id,fact_id), UNIQUE(id,document_set_id,filing_id),
    FOREIGN KEY(cell_id,filing_id,table_id) REFERENCES sec.table_cells(id,filing_id,table_id),
    FOREIGN KEY(occurrence_id,document_set_id,fact_id,filing_id) REFERENCES sec.fact_source_occurrences(id,document_set_id,fact_id,filing_id)
);
CREATE TABLE sec.source_quality_issues (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filing_id uuid NOT NULL, document_set_id uuid NOT NULL,
    issue_key text NOT NULL CHECK(btrim(issue_key)<>''), binding_id uuid NOT NULL,
    code text NOT NULL CHECK(code IN ('instant_context_under_duration_header','opening_balance_date_not_prior_day')),
    rule_version text NOT NULL CHECK(rule_version='cash-period-review-v1'),
    expected jsonb NOT NULL CHECK(jsonb_typeof(expected)='object'), observed jsonb NOT NULL CHECK(jsonb_typeof(observed)='object'),
    evidence jsonb NOT NULL CHECK(jsonb_typeof(evidence)='object'), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_set_id,issue_key), UNIQUE(id,filing_id),
    FOREIGN KEY(binding_id,document_set_id,filing_id) REFERENCES sec.cell_source_fact_bindings(id,document_set_id,filing_id),
    CHECK(expected<>observed)
);
CREATE TABLE sec.source_processing_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filing_id uuid NOT NULL REFERENCES sec.filings(id),
    set_key text NOT NULL CHECK(btrim(set_key)<>''), document_set_id uuid NOT NULL,
    contract_version integer NOT NULL DEFAULT 2 CHECK(contract_version=2),
    versions jsonb NOT NULL CHECK(versions='{"facts":"facts-v1","tables":"tables-v4","reports":"report-v1","chunks":"paragraph-v1"}'::jsonb),
    source_checks jsonb NOT NULL CHECK(jsonb_typeof(source_checks)='object'),
    semantic_checks jsonb NOT NULL CHECK(jsonb_typeof(semantic_checks)='object'),
    source_validation_status text NOT NULL DEFAULT 'passed' CHECK(source_validation_status='passed'),
    validation_status text NOT NULL CHECK(validation_status IN ('passed','review_required')),
    coverage jsonb NOT NULL CHECK(jsonb_typeof(coverage)='object'), configuration jsonb NOT NULL CHECK(jsonb_typeof(configuration)='object'),
    counts jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(counts)='object'),
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(filing_id,set_key), UNIQUE(id,filing_id),
    FOREIGN KEY(document_set_id,filing_id) REFERENCES sec.source_document_sets(id,filing_id)
);
CREATE TABLE sec.source_processing_tables (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), processing_set_id uuid NOT NULL, filing_id uuid NOT NULL, table_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(processing_set_id,table_id),
    FOREIGN KEY(processing_set_id,filing_id) REFERENCES sec.source_processing_sets(id,filing_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(table_id,filing_id) REFERENCES sec.financial_tables(id,filing_id)
);
CREATE TABLE sec.source_processing_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), processing_set_id uuid NOT NULL, filing_id uuid NOT NULL, report_id uuid NOT NULL,
    chunking_version text NOT NULL CHECK(chunking_version='paragraph-v1'), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(processing_set_id,report_id),
    FOREIGN KEY(processing_set_id,filing_id) REFERENCES sec.source_processing_sets(id,filing_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(report_id,filing_id) REFERENCES sec.reports(id,filing_id)
);
CREATE TABLE sec.source_processing_issues (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), processing_set_id uuid NOT NULL, filing_id uuid NOT NULL, issue_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(processing_set_id,issue_id),
    FOREIGN KEY(processing_set_id,filing_id) REFERENCES sec.source_processing_sets(id,filing_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(issue_id,filing_id) REFERENCES sec.source_quality_issues(id,filing_id)
);
CREATE INDEX source_sets_xml_idx ON sec.source_document_sets(xml_document_id);
CREATE INDEX source_members_document_idx ON sec.source_document_set_members(source_document_id);
CREATE INDEX source_occurrences_fact_idx ON sec.fact_source_occurrences(fact_id);
CREATE INDEX source_bindings_table_idx ON sec.cell_source_fact_bindings(table_id);
CREATE INDEX source_issues_binding_idx ON sec.source_quality_issues(binding_id);
CREATE INDEX source_processing_document_set_idx ON sec.source_processing_sets(document_set_id);

CREATE FUNCTION sec.guard_v4_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sec.lock_processing_filing(OLD.filing_id);
    IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'V4 source evidence and selections are immutable; create a new versioned identity' USING ERRCODE='23514';
END; $$;
CREATE FUNCTION sec.guard_v4_member_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sealed boolean; identical boolean;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    IF TG_TABLE_NAME='source_document_set_members' THEN
        sealed := EXISTS(SELECT 1 FROM sec.source_document_sets WHERE id=NEW.document_set_id);
    ELSE
        sealed := EXISTS(SELECT 1 FROM sec.source_processing_sets WHERE id=NEW.processing_set_id);
    END IF;
    IF sealed THEN
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM sec.%I x WHERE to_jsonb(x)-ARRAY[''id'',''created_at'']= $1-ARRAY[''id'',''created_at''])',TG_TABLE_NAME)
            INTO identical USING to_jsonb(NEW);
        IF NOT identical THEN RAISE EXCEPTION 'A sealed V4 selection cannot acquire new members' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;
CREATE FUNCTION sec.guard_document_set() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE primary_source sec.source_documents%ROWTYPE; xml_source sec.source_documents%ROWTYPE;
        index_source sec.source_documents%ROWTYPE; members jsonb; urls jsonb; archive text;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO primary_source FROM sec.source_documents WHERE id=NEW.primary_html_document_id AND filing_id=NEW.filing_id;
    SELECT * INTO xml_source FROM sec.source_documents WHERE id=NEW.xml_document_id AND filing_id=NEW.filing_id;
    SELECT * INTO index_source FROM sec.source_documents WHERE id=NEW.index_document_id AND filing_id=NEW.filing_id;
    IF primary_source.role IS DISTINCT FROM 'main_inline_xbrl_filing' OR xml_source.role IS DISTINCT FROM 'extracted_xbrl_instance'
        OR index_source.role IS DISTINCT FROM 'filing_index' THEN
        RAISE EXCEPTION 'Document set sources must retain their distinct registered roles' USING ERRCODE='23514';
    END IF;
    archive := regexp_replace(index_source.source_url,'[^/]+$','');
    IF archive !~ '^https://www[.]sec[.]gov/Archives/edgar/data/[1-9][0-9]*/[0-9]{18}/$'
        OR left(primary_source.source_url,length(archive))<>archive OR left(xml_source.source_url,length(archive))<>archive THEN
        RAISE EXCEPTION 'Document set sources must belong to the same SEC accession archive' USING ERRCODE='23514';
    END IF;
    IF (SELECT count(*) FROM sec.source_document_set_members WHERE document_set_id=NEW.id AND is_primary)<>1
        OR NOT EXISTS(SELECT 1 FROM sec.source_document_set_members WHERE document_set_id=NEW.id AND is_primary AND source_document_id=NEW.primary_html_document_id)
        OR EXISTS(SELECT 1 FROM sec.source_document_set_members m JOIN sec.source_documents d ON d.id=m.source_document_id
            WHERE m.document_set_id=NEW.id AND (m.filing_id<>NEW.filing_id OR m.source_sha256<>d.sha256
            OR m.index_locator->>'source_sha256' IS DISTINCT FROM index_source.sha256
            OR nullif(btrim(m.index_locator->>'source_xpath'),'') IS NULL
            OR left(d.source_url,length(archive))<>archive
            OR (m.is_primary AND (d.role<>'main_inline_xbrl_filing' OR m.document_type<>(SELECT form FROM sec.filings WHERE id=NEW.filing_id)))
            OR (NOT m.is_primary AND d.role<>'supplemental_inline_xbrl_document'))) THEN
        RAISE EXCEPTION 'Document membership differs from registered source or index evidence' USING ERRCODE='23514';
    END IF;
    SELECT jsonb_agg(jsonb_build_object('source_document_id',m.source_document_id,'source_url',d.source_url,
        'sha256',d.sha256,'sequence',m.sequence,'document_type',m.document_type,'is_primary',m.is_primary,'index_locator',m.index_locator) ORDER BY m.sequence),
        jsonb_agg(to_jsonb(d.source_url) ORDER BY m.sequence)
    INTO members,urls FROM sec.source_document_set_members m JOIN sec.source_documents d ON d.id=m.source_document_id WHERE m.document_set_id=NEW.id;
    IF NEW.evidence->'member_source_urls' IS DISTINCT FROM urls OR NEW.evidence->>'inline_target' IS DISTINCT FROM ''
        OR NEW.evidence#>'{source_checks,index_membership_complete}' IS DISTINCT FROM 'true'::jsonb
        OR NEW.evidence#>'{source_checks,single_primary_target}' IS DISTINCT FROM 'true'::jsonb
        OR NEW.evidence#>'{source_checks,excluded_targets_separate}' IS DISTINCT FROM 'true'::jsonb
        OR jsonb_typeof(NEW.evidence->'excluded_inline_documents') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Document-set membership/target proof is missing or inconsistent' USING ERRCODE='23514';
    END IF;
    NEW.membership_sha256 := encode(sha256(convert_to(jsonb_build_object('manifest',NEW.manifest_sha256,'policy',NEW.policy_version,
        'xml',jsonb_build_object('id',xml_source.id,'sha256',xml_source.sha256),'index',jsonb_build_object('id',index_source.id,'sha256',index_source.sha256),
        'members',members)::text,'UTF8')),'hex');
    RETURN NEW;
END; $$;
CREATE TRIGGER source_document_sets_check BEFORE INSERT ON sec.source_document_sets FOR EACH ROW EXECUTE FUNCTION sec.guard_document_set();

CREATE FUNCTION sec.guard_source_occurrence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ds sec.source_document_sets%ROWTYPE; fact sec.financial_facts%ROWTYPE; source_hash text; target_hash text; period jsonb; fragment jsonb; ordinal integer:=0;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    IF EXISTS(SELECT 1 FROM sec.source_processing_sets WHERE document_set_id=NEW.document_set_id) THEN
        IF EXISTS(SELECT 1 FROM sec.fact_source_occurrences occurrence
            WHERE occurrence.document_set_id=NEW.document_set_id AND occurrence.fact_id=NEW.fact_id
              AND to_jsonb(occurrence)-ARRAY['id','created_at']=to_jsonb(NEW)-ARRAY['id','created_at']) THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'Source occurrences are sealed by their V4 processing set' USING ERRCODE='23514';
    END IF;
    SELECT * INTO ds FROM sec.source_document_sets WHERE id=NEW.document_set_id AND filing_id=NEW.filing_id;
    SELECT * INTO fact FROM sec.financial_facts WHERE id=NEW.fact_id AND filing_id=NEW.filing_id;
    SELECT m.source_sha256 INTO source_hash FROM sec.source_document_set_members m WHERE m.document_set_id=NEW.document_set_id AND m.source_document_id=NEW.html_document_id;
    SELECT sha256 INTO target_hash FROM sec.source_documents WHERE id=ds.xml_document_id;
    period := CASE fact.period_kind WHEN 'instant' THEN jsonb_build_object('instant',fact.instant_date::text)
        WHEN 'duration' THEN jsonb_build_object('startDate',fact.period_start::text,'endDate',fact.period_end::text) ELSE jsonb_build_object('forever','') END;
    IF ds.id IS NULL OR fact.id IS NULL OR source_hash IS NULL OR fact.source_document_id<>ds.xml_document_id OR fact.extraction_version<>'facts-v1'
        OR NEW.inline_id IS DISTINCT FROM fact.occurrence_key OR NEW.evidence->>'xml_id' IS DISTINCT FROM NEW.inline_id
        OR NEW.evidence->>'html_sha256' IS DISTINCT FROM source_hash OR NEW.evidence->>'xml_sha256' IS DISTINCT FROM target_hash
        OR NEW.evidence->>'concept' IS DISTINCT FROM ('{'||fact.concept_namespace||'}'||fact.concept_name)
        OR NEW.evidence->'period' IS DISTINCT FROM period OR NEW.evidence->'dimensions' IS DISTINCT FROM fact.dimensions
        OR nullif(NEW.evidence->'unit','null'::jsonb) IS DISTINCT FROM fact.unit
        OR NEW.evidence->'is_nil' IS DISTINCT FROM to_jsonb(fact.is_nil)
        OR NEW.evidence->'inline_is_nil' IS DISTINCT FROM to_jsonb(fact.is_nil)
        OR NEW.evidence->>'context_ref' IS DISTINCT FROM fact.source_reference->>'context_ref'
        OR NEW.evidence->>'unit_ref' IS DISTINCT FROM fact.source_reference->>'unit_ref'
        OR (NOT fact.is_nil AND NEW.evidence->>'raw_value' IS DISTINCT FROM fact.raw_value)
        OR (fact.is_nil AND (NEW.evidence->'inline_mapped_value' IS DISTINCT FROM 'null'::jsonb OR nullif(NEW.evidence->>'raw_value','') IS NOT NULL)) THEN
        RAISE EXCEPTION 'Source occurrence differs from the original stored XML fact/scope' USING ERRCODE='23514';
    END IF;
    IF coalesce(NEW.evidence->>'value_comparison','exact') NOT IN ('exact','escaped-markup-infoset-v1') THEN
        RAISE EXCEPTION 'Unknown source value comparator' USING ERRCODE='23514';
    END IF;
    IF NEW.evidence->>'value_comparison'='escaped-markup-infoset-v1' AND (
        fact.is_nil OR fact.unit IS NOT NULL OR NEW.evidence->>'inline_kind' IS DISTINCT FROM 'nonnumeric'
        OR NEW.evidence->'inline_escape' IS DISTINCT FROM 'true'::jsonb
        OR coalesce(NEW.evidence->>'inline_canonical_sha256','') !~ '^[0-9a-f]{64}$'
        OR coalesce(NEW.evidence->>'xml_canonical_sha256','') !~ '^[0-9a-f]{64}$'
        OR NEW.evidence->>'inline_canonical_sha256' IS DISTINCT FROM NEW.evidence->>'xml_canonical_sha256') THEN
        RAISE EXCEPTION 'Escaped markup requires exact canonical source/XML content' USING ERRCODE='23514';
    END IF;
    IF NOT fact.is_nil AND ((fact.unit IS NULL AND coalesce(NEW.evidence->>'value_comparison','exact')='exact'
            AND NEW.evidence->>'inline_mapped_value' IS DISTINCT FROM fact.raw_value)
        OR (fact.unit IS NOT NULL AND ((NEW.evidence->>'inline_mapped_value')::numeric IS DISTINCT FROM fact.numeric_value
             OR (NEW.evidence->>'raw_value')::numeric IS DISTINCT FROM fact.numeric_value))) THEN
        RAISE EXCEPTION 'Inline/XML value mismatch is a hard source failure' USING ERRCODE='23514';
    END IF;
    FOR fragment IN SELECT value FROM jsonb_array_elements(NEW.fragments) LOOP
        IF fragment->>'fragment_order' IS DISTINCT FROM ordinal::text OR nullif(btrim(fragment->>'source_xpath'),'') IS NULL
            OR NOT EXISTS(SELECT 1 FROM sec.source_document_set_members WHERE document_set_id=NEW.document_set_id
                          AND source_document_id=(fragment->>'source_document_id')::uuid) THEN
            RAISE EXCEPTION 'Continuation fragments must retain ordered member-document locations' USING ERRCODE='23514';
        END IF;
        ordinal:=ordinal+1;
    END LOOP;
    RETURN NEW;
END; $$;
CREATE TRIGGER fact_source_occurrences_check BEFORE INSERT ON sec.fact_source_occurrences FOR EACH ROW EXECUTE FUNCTION sec.guard_source_occurrence();

CREATE FUNCTION sec.guard_review_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cell sec.table_cells%ROWTYPE; parent sec.financial_tables%ROWTYPE; occurrence sec.fact_source_occurrences%ROWTYPE; retained jsonb;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO cell FROM sec.table_cells WHERE id=NEW.cell_id AND table_id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT * INTO parent FROM sec.financial_tables WHERE id=NEW.table_id AND filing_id=NEW.filing_id;
    SELECT * INTO occurrence FROM sec.fact_source_occurrences WHERE id=NEW.occurrence_id AND fact_id=NEW.fact_id
        AND document_set_id=NEW.document_set_id AND filing_id=NEW.filing_id;
    SELECT value INTO retained FROM sec.table_structure_cells(parent.structure) value WHERE value->>'cell_id'=cell.cell_key;
    IF sec.table_scope_is_sealed(NEW.table_id) OR cell.id IS NULL OR occurrence.id IS NULL
        OR cell.kind<>'data' OR cell.resolution_status<>'review_required' OR parent.extraction_version<>'tables-v4'
        OR parent.relational_cells_version IS DISTINCT FROM 3 OR parent.structure->>'schema_version' IS DISTINCT FROM '3'
        OR occurrence.html_document_id<>parent.source_document_id
        OR NEW.evidence IS DISTINCT FROM retained->'source_fact_references'->NEW.reference_order
        OR NEW.evidence->>'method' IS DISTINCT FROM 'inline_id'
        OR left(occurrence.source_xpath,length(cell.source_locator->>'xpath')+1) IS DISTINCT FROM (cell.source_locator->>'xpath')||'/'
        OR NEW.evidence->>'inline_document_id' IS DISTINCT FROM occurrence.html_document_id::text
        OR NEW.evidence->>'inline_xpath' IS DISTINCT FROM occurrence.evidence->>'inline_xpath'
        OR NEW.evidence->>'xml_id' IS DISTINCT FROM occurrence.inline_id
        OR EXISTS(SELECT 1 FROM sec.cell_fact_links WHERE cell_id=NEW.cell_id) THEN
        RAISE EXCEPTION 'Review binding requires exact unaccepted V4 cell/source occurrence evidence' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM jsonb_each(occurrence.evidence) e
        WHERE e.key IN ('concept','context_ref','unit_ref','unit','period','dimensions','raw_value','is_nil')
          AND NEW.evidence->e.key IS DISTINCT FROM e.value) THEN
        RAISE EXCEPTION 'Review binding cannot reinterpret the original XML fact' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER cell_source_fact_bindings_check BEFORE INSERT ON sec.cell_source_fact_bindings FOR EACH ROW EXECUTE FUNCTION sec.guard_review_binding();

CREATE FUNCTION sec.guard_source_quality_issue() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE binding sec.cell_source_fact_bindings%ROWTYPE; cell sec.table_cells%ROWTYPE;
        fact sec.financial_facts%ROWTYPE; retained jsonb; expected_issue jsonb;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO binding FROM sec.cell_source_fact_bindings WHERE id=NEW.binding_id AND filing_id=NEW.filing_id AND document_set_id=NEW.document_set_id;
    SELECT * INTO cell FROM sec.table_cells WHERE id=binding.cell_id;
    SELECT * INTO fact FROM sec.financial_facts WHERE id=binding.fact_id;
    SELECT value INTO retained FROM sec.financial_tables t CROSS JOIN LATERAL sec.table_structure_cells(t.structure) value
        WHERE t.id=cell.table_id AND value->>'cell_id'=cell.cell_key;
    SELECT value INTO expected_issue FROM jsonb_array_elements(retained->'quality_issues') value WHERE value->>'issue_key'=NEW.issue_key;
    IF binding.id IS NULL OR sec.table_scope_is_sealed(binding.table_id) OR fact.period_kind<>'instant'
        OR NEW.expected->'period' IS DISTINCT FROM cell.column_period
        OR NEW.observed->'period' IS DISTINCT FROM jsonb_build_object('instant',fact.instant_date::text)
        OR NOT (cell.column_period ?& ARRAY['startDate','endDate'])
        OR expected_issue IS NULL OR NEW.code IS DISTINCT FROM expected_issue->>'code'
        OR NEW.rule_version IS DISTINCT FROM expected_issue->>'rule_version'
        OR NEW.expected IS DISTINCT FROM expected_issue->'expected' OR NEW.observed IS DISTINCT FROM expected_issue->'observed'
        OR NEW.evidence IS DISTINCT FROM expected_issue->'evidence' THEN
        RAISE EXCEPTION 'Quality issue must retain the exact bound source/header conflict' USING ERRCODE='23514';
    END IF;
    IF NEW.code='opening_balance_date_not_prior_day' AND (
        cell.row_heading !~* '(beginning|opening|start of)' OR fact.instant_date IS DISTINCT FROM (cell.column_period->>'startDate')::date
        OR fact.concept_namespace !~ '^https?://(fasb[.]org|xbrl[.]us)/us-gaap/'
        OR fact.concept_name NOT IN ('CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
             'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations')) THEN
        RAISE EXCEPTION 'Opening-date issue requires explicit cash source evidence at the reported duration start' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER source_quality_issues_check BEFORE INSERT ON sec.source_quality_issues FOR EACH ROW EXECUTE FUNCTION sec.guard_source_quality_issue();

CREATE FUNCTION sec.validate_source_processing_set() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ds sec.source_document_sets%ROWTYPE; fact_count bigint; document_count bigint; table_count bigint;
        report_count bigint; chunk_count bigint; cell_count bigint; accepted_count bigint; source_only_count bigint;
        review_count bigint; issue_count bigint; target_table uuid;
BEGIN
    PERFORM sec.lock_processing_filing(NEW.filing_id);
    SELECT * INTO ds FROM sec.source_document_sets WHERE id=NEW.document_set_id AND filing_id=NEW.filing_id;
    IF ds.id IS NULL OR NOT (NEW.source_checks ?& ARRAY['document_set_complete','union_correspondence','numeric_values_compared',
        'source_cells_complete','source_bindings_exact','reports_complete','chunks_complete'])
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.source_checks) c WHERE btrim(c.key)='' OR c.value IS DISTINCT FROM 'true'::jsonb)
        OR NEW.semantic_checks='{}' OR NOT(NEW.semantic_checks ? 'period_header_consistency')
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.semantic_checks) c WHERE btrim(c.key)='' OR c.value NOT IN ('true'::jsonb,'false'::jsonb))
        OR NEW.coverage-ARRAY['facts','tables','reports','chunks']<>'{}'::jsonb
        OR NOT(NEW.coverage ?& ARRAY['facts','tables','reports','chunks'])
        OR EXISTS(SELECT 1 FROM jsonb_each_text(NEW.coverage) c WHERE c.value NOT IN ('partial','complete')
            AND NOT(c.key='reports' AND c.value='unknown')) THEN
        RAISE EXCEPTION 'V4 sets require complete explicit source checks and separate semantic assessment' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO fact_count FROM sec.financial_facts WHERE source_document_id=ds.xml_document_id AND extraction_version=NEW.versions->>'facts';
    IF fact_count=0 OR EXISTS((SELECT id FROM sec.financial_facts WHERE source_document_id=ds.xml_document_id AND extraction_version=NEW.versions->>'facts'
        EXCEPT SELECT fact_id FROM sec.fact_source_occurrences WHERE document_set_id=ds.id)
        UNION ALL (SELECT fact_id FROM sec.fact_source_occurrences WHERE document_set_id=ds.id
        EXCEPT SELECT id FROM sec.financial_facts WHERE source_document_id=ds.xml_document_id AND extraction_version=NEW.versions->>'facts')) THEN
        RAISE EXCEPTION 'V4 union correspondence must cover every selected XML occurrence exactly' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO document_count FROM sec.source_document_set_members WHERE document_set_id=ds.id;
    SELECT count(*) INTO table_count FROM sec.source_processing_tables WHERE processing_set_id=NEW.id;
    SELECT count(*) INTO report_count FROM sec.source_processing_reports WHERE processing_set_id=NEW.id;
    IF table_count=0 OR report_count<>document_count
        OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.financial_tables t ON t.id=member.table_id
            WHERE member.processing_set_id=NEW.id AND (t.extraction_version<>NEW.versions->>'tables' OR t.relational_cells_version IS DISTINCT FROM 3
             OR t.structure->>'schema_version' IS DISTINCT FROM '3' OR t.filing_id<>NEW.filing_id
             OR t.structure#>>'{fact_scope,xml_document_id}' IS DISTINCT FROM ds.xml_document_id::text
             OR t.structure#>>'{fact_scope,facts_version}' IS DISTINCT FROM NEW.versions->>'facts'
             OR NOT EXISTS(SELECT 1 FROM sec.source_document_set_members m WHERE m.document_set_id=ds.id AND m.source_document_id=t.source_document_id)))
        OR EXISTS((SELECT t.id FROM sec.financial_tables t JOIN sec.source_document_set_members m ON m.source_document_id=t.source_document_id
                   WHERE m.document_set_id=ds.id AND t.extraction_version=NEW.versions->>'tables'
                   EXCEPT SELECT table_id FROM sec.source_processing_tables WHERE processing_set_id=NEW.id)) THEN
        RAISE EXCEPTION 'V4 table selection must retain every schema3 member-source table' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM sec.source_processing_reports member JOIN sec.reports r ON r.id=member.report_id
        WHERE member.processing_set_id=NEW.id AND (r.filing_id<>NEW.filing_id OR r.extraction_version<>NEW.versions->>'reports'
          OR NOT EXISTS(SELECT 1 FROM sec.source_document_set_members m WHERE m.document_set_id=ds.id AND m.source_document_id=r.source_document_id)
          OR NOT EXISTS(SELECT 1 FROM sec.chunks c WHERE c.report_id=r.id AND c.chunking_version=member.chunking_version)))
        OR (SELECT count(DISTINCT r.source_document_id) FROM sec.source_processing_reports member JOIN sec.reports r ON r.id=member.report_id
            WHERE member.processing_set_id=NEW.id)<>document_count THEN
        RAISE EXCEPTION 'V4 reports/chunks must cover each original HTML document separately' USING ERRCODE='23514';
    END IF;
    FOR target_table IN SELECT table_id FROM sec.source_processing_tables WHERE processing_set_id=NEW.id LOOP
        PERFORM sec.validate_table_cell_consistency(target_table);
    END LOOP;
    IF EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.cell_fact_links link ON link.table_id=member.table_id
        JOIN sec.financial_facts fact ON fact.id=link.fact_id WHERE member.processing_set_id=NEW.id
        AND (fact.source_document_id<>ds.xml_document_id OR fact.extraction_version<>NEW.versions->>'facts'))
        OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.financial_tables t ON t.id=member.table_id
            JOIN sec.cell_fact_links link ON link.table_id=t.id WHERE member.processing_set_id=NEW.id
            AND NOT EXISTS(SELECT 1 FROM sec.fact_source_occurrences occurrence
                WHERE occurrence.document_set_id=ds.id AND occurrence.fact_id=link.fact_id
                  AND occurrence.html_document_id=t.source_document_id
                  AND occurrence.evidence->>'inline_xpath'=link.evidence->>'inline_xpath'))
        OR EXISTS(SELECT 1 FROM sec.source_processing_tables member JOIN sec.cell_source_fact_bindings binding ON binding.table_id=member.table_id
            WHERE member.processing_set_id=NEW.id AND binding.document_set_id<>ds.id)
        OR EXISTS((SELECT issue.id FROM sec.source_quality_issues issue JOIN sec.cell_source_fact_bindings binding ON binding.id=issue.binding_id
                   JOIN sec.source_processing_tables member ON member.table_id=binding.table_id WHERE member.processing_set_id=NEW.id
                   EXCEPT SELECT issue_id FROM sec.source_processing_issues WHERE processing_set_id=NEW.id)
                  UNION ALL
                  (SELECT issue_id FROM sec.source_processing_issues WHERE processing_set_id=NEW.id EXCEPT
                   SELECT issue.id FROM sec.source_quality_issues issue JOIN sec.cell_source_fact_bindings binding ON binding.id=issue.binding_id
                   JOIN sec.source_processing_tables member ON member.table_id=binding.table_id WHERE member.processing_set_id=NEW.id AND issue.document_set_id=ds.id)) THEN
        RAISE EXCEPTION 'V4 selected fact links and quality issues must match their exact document-set scope' USING ERRCODE='23514';
    END IF;
    SELECT count(*) FILTER(WHERE c.kind='data'), count(*) FILTER(WHERE c.kind='data' AND c.resolution_status='resolved'),
        count(*) FILTER(WHERE c.resolution_status='source_only'), count(*) FILTER(WHERE c.resolution_status='review_required')
    INTO cell_count,accepted_count,source_only_count,review_count
    FROM sec.source_processing_tables member JOIN sec.table_cells c ON c.table_id=member.table_id WHERE member.processing_set_id=NEW.id;
    SELECT count(*) INTO issue_count FROM sec.source_processing_issues WHERE processing_set_id=NEW.id;
    IF cell_count=0 OR cell_count<>accepted_count+source_only_count+review_count OR (review_count=0) IS DISTINCT FROM (issue_count=0)
        OR NEW.semantic_checks->'period_header_consistency' IS DISTINCT FROM to_jsonb(issue_count=0)
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.semantic_checks) c WHERE c.value='false'::jsonb AND c.key<>'period_header_consistency') THEN
        RAISE EXCEPTION 'Every V4 financial cell and semantic exception must be explicitly accounted for' USING ERRCODE='23514';
    END IF;
    SELECT count(*) INTO chunk_count FROM sec.source_processing_reports member JOIN sec.chunks c
        ON c.report_id=member.report_id AND c.chunking_version=member.chunking_version WHERE member.processing_set_id=NEW.id;
    NEW.validation_status:=CASE WHEN issue_count=0 THEN 'passed' ELSE 'review_required' END;
    NEW.counts:=jsonb_build_object('inline_documents',document_count,'facts',fact_count,'tables',table_count,'reports',report_count,'chunks',chunk_count,
        'financial_cells',cell_count,'accepted_cells',accepted_count,'source_only_cells',source_only_count,
        'review_required_cells',review_count,'quality_issues',issue_count,'unaccounted_cells',0);
    RETURN NEW;
END; $$;
CREATE TRIGGER source_processing_sets_check BEFORE INSERT ON sec.source_processing_sets FOR EACH ROW EXECUTE FUNCTION sec.validate_source_processing_set();

-- Evidence records and member lists cannot be amended after creation.
DO $$ DECLARE table_name text; BEGIN
    FOREACH table_name IN ARRAY ARRAY['source_document_sets','source_document_set_members','fact_source_occurrences',
        'cell_source_fact_bindings','source_quality_issues','source_processing_sets','source_processing_tables','source_processing_reports','source_processing_issues'] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON sec.%I FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_immutable()',table_name||'_immutable',table_name);
    END LOOP;
    FOREACH table_name IN ARRAY ARRAY['source_document_set_members','source_processing_tables','source_processing_reports','source_processing_issues'] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON sec.%I FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_member_insert()',table_name||'_closed_members',table_name);
    END LOOP;
END $$;

CREATE FUNCTION sec.v4_processing_artifact_is_sealed(artifact_table text, artifact jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    CASE artifact_table
    WHEN 'financial_facts' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_processing_sets p JOIN sec.source_document_sets ds ON ds.id=p.document_set_id
            WHERE ds.xml_document_id=(artifact->>'source_document_id')::uuid AND p.versions->>'facts'=artifact->>'extraction_version');
    WHEN 'financial_tables' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_processing_sets p JOIN sec.source_document_set_members m ON m.document_set_id=p.document_set_id
            WHERE m.source_document_id=(artifact->>'source_document_id')::uuid AND p.versions->>'tables'=artifact->>'extraction_version');
    WHEN 'reports' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_processing_sets p JOIN sec.source_document_set_members m ON m.document_set_id=p.document_set_id
            WHERE m.source_document_id=(artifact->>'source_document_id')::uuid AND p.versions->>'reports'=artifact->>'extraction_version');
    WHEN 'report_sections' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_processing_reports WHERE report_id=(artifact->>'report_id')::uuid);
    WHEN 'chunks' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_processing_reports WHERE report_id=(artifact->>'report_id')::uuid
            AND chunking_version=artifact->>'chunking_version');
    WHEN 'financial_table_facts' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_processing_tables WHERE table_id=(artifact->>'table_id')::uuid);
    WHEN 'source_documents' THEN
        RETURN EXISTS(SELECT 1 FROM sec.source_document_sets ds WHERE (artifact->>'id')::uuid IN (ds.xml_document_id,ds.index_document_id,ds.primary_html_document_id))
            OR EXISTS(SELECT 1 FROM sec.source_document_set_members WHERE source_document_id=(artifact->>'id')::uuid)
            OR EXISTS(SELECT 1 FROM sec.source_processing_sets p WHERE p.filing_id=(artifact->>'filing_id')::uuid
                AND p.configuration->'source_hashes' ? (artifact->>'local_path'));
    ELSE RETURN false;
    END CASE;
END; $$;

CREATE FUNCTION sec.guard_v4_source_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sec.lock_processing_filing(OLD.filing_id);
    IF sec.v4_processing_artifact_is_sealed('source_documents',to_jsonb(OLD)) AND
        (TG_OP='DELETE' OR to_jsonb(OLD)-ARRAY['storage_uri','title','document_type'] IS DISTINCT FROM to_jsonb(NEW)-ARRAY['storage_uri','title','document_type']) THEN
        RAISE EXCEPTION 'A document-set member retains its original identity, hash and retrieval receipt' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END; $$;
CREATE TRIGGER source_documents_v4_receipt BEFORE UPDATE OR DELETE ON sec.source_documents FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_source_receipt();
CREATE FUNCTION sec.guard_v4_occurrence_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sec.lock_processing_filing(OLD.filing_id);
    IF EXISTS(SELECT 1 FROM sec.fact_source_occurrences WHERE fact_id=OLD.id) THEN
        IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'An independently sourced fact cannot change underneath its occurrence proof' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END; $$;
CREATE TRIGGER financial_facts_v4_occurrence BEFORE UPDATE OR DELETE ON sec.financial_facts FOR EACH ROW EXECUTE FUNCTION sec.guard_v4_occurrence_fact();
CREATE FUNCTION sec.guard_v4_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM sec.source_document_sets) OR EXISTS(SELECT 1 FROM sec.source_processing_sets) THEN
        RAISE EXCEPTION 'Truncation would bypass immutable V4 source evidence or artifact selections' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END; $$;
DO $$ DECLARE table_name text; BEGIN
    FOREACH table_name IN ARRAY ARRAY['source_document_sets','source_document_set_members','fact_source_occurrences','cell_source_fact_bindings',
        'source_quality_issues','source_processing_sets','source_processing_tables','source_processing_reports','source_processing_issues',
        'source_documents','financial_facts','financial_tables','table_cells','cell_fact_links','cell_source_observations','financial_table_facts','reports','report_sections','chunks'] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON sec.%I FOR EACH STATEMENT EXECUTE FUNCTION sec.guard_v4_truncate()',table_name||'_v4_no_truncate',table_name);
    END LOOP;
END $$;

CREATE VIEW sec.source_processing_overview AS
SELECT p.id AS processing_set_id,p.filing_id,p.set_key,p.contract_version,p.document_set_id,p.versions,p.source_validation_status,
    p.validation_status,p.coverage,p.counts,p.source_checks,p.semantic_checks,p.configuration,p.created_at,
    ds.manifest_sha256,ds.membership_sha256,ds.xml_document_id,ds.primary_html_document_id,
    f.accession_number,f.form,f.filing_date,f.company_name_as_filed,c.id AS company_id,c.cik,c.name AS company_name
FROM sec.source_processing_sets p JOIN sec.source_document_sets ds ON ds.id=p.document_set_id
JOIN sec.filings f ON f.id=p.filing_id JOIN sec.companies c ON c.id=f.company_id;

CREATE VIEW sec.source_statement_cells AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.source_validation_status,p.coverage,p.counts,
    c.*,t.table_key,t.statement_type,t.extraction_version,t.source_document_id,s.source_url,s.sha256 AS source_sha256,
    f.accession_number,f.form,f.company_name_as_filed,issuer.id AS company_id,issuer.cik,issuer.name AS company_name,
    coalesce(issues.issue_count,0) AS quality_issue_count,coalesce(issues.issue_codes,ARRAY[]::text[]) AS quality_issue_codes,
    coalesce(issues.quality_issues,'[]'::jsonb) AS quality_issues,coalesce(bindings.source_facts,'[]'::jsonb) AS review_source_facts,
    coalesce(accepted.accepted_facts,'[]'::jsonb) AS accepted_facts
FROM sec.source_processing_sets p JOIN sec.source_processing_tables member ON member.processing_set_id=p.id
JOIN sec.financial_tables t ON t.id=member.table_id JOIN sec.table_cells c ON c.table_id=t.id
JOIN sec.source_documents s ON s.id=t.source_document_id JOIN sec.filings f ON f.id=p.filing_id
JOIN sec.companies issuer ON issuer.id=f.company_id
LEFT JOIN LATERAL (SELECT count(*) AS issue_count,array_agg(q.code ORDER BY q.issue_key) AS issue_codes,
    jsonb_agg(jsonb_build_object('issue_id',q.id,'issue_key',q.issue_key,'code',q.code,'rule_version',q.rule_version,
                                'expected',q.expected,'observed',q.observed,'evidence',q.evidence) ORDER BY q.issue_key) AS quality_issues
    FROM sec.source_quality_issues q JOIN sec.cell_source_fact_bindings b ON b.id=q.binding_id
    JOIN sec.source_processing_issues selected ON selected.issue_id=q.id AND selected.processing_set_id=p.id WHERE b.cell_id=c.id) issues ON true
LEFT JOIN LATERAL (SELECT jsonb_agg(jsonb_build_object('binding_id',b.id,'fact_id',fact.id,'occurrence_id',occ.id,
    'concept_namespace',fact.concept_namespace,'concept_name',fact.concept_name,'numeric_value',fact.numeric_value,'raw_value',fact.raw_value,
    'period_kind',fact.period_kind,'period_start',fact.period_start,'period_end',fact.period_end,'instant_date',fact.instant_date,
    'html_document_id',occ.html_document_id,'source_xpath',occ.source_xpath,'inline_id',occ.inline_id,'evidence',b.evidence)
    ORDER BY b.reference_order) AS source_facts FROM sec.cell_source_fact_bindings b
    JOIN sec.financial_facts fact ON fact.id=b.fact_id JOIN sec.fact_source_occurrences occ ON occ.id=b.occurrence_id WHERE b.cell_id=c.id) bindings ON true
LEFT JOIN LATERAL (SELECT jsonb_agg(jsonb_build_object('fact_id',l.fact_id,'link_method',l.link_method,'evidence',l.evidence)
    ORDER BY l.reference_order) AS accepted_facts FROM sec.cell_fact_links l WHERE l.cell_id=c.id) accepted ON true;

CREATE VIEW sec.analysis_ready_statement_cells AS SELECT * FROM sec.source_statement_cells
WHERE validation_status='passed' AND quality_issue_count=0 AND resolution_status<>'review_required';
CREATE VIEW sec.source_fact_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.source_validation_status,p.validation_status,p.coverage,
    fact.*,occ.html_document_id,occ.inline_id,occ.source_xpath,occ.evidence AS occurrence_evidence,occ.fragments,
    s.source_url AS inline_source_url,s.sha256 AS inline_source_sha256,
    f.accession_number,f.form,f.company_name_as_filed,issuer.cik,issuer.name AS company_name,
    coalesce(issues.issue_count,0) AS quality_issue_count,coalesce(issues.issue_codes,ARRAY[]::text[]) AS quality_issue_codes
FROM sec.source_processing_sets p JOIN sec.source_document_sets ds ON ds.id=p.document_set_id
JOIN sec.financial_facts fact ON fact.source_document_id=ds.xml_document_id AND fact.extraction_version=p.versions->>'facts'
JOIN sec.fact_source_occurrences occ ON occ.document_set_id=ds.id AND occ.fact_id=fact.id
JOIN sec.source_documents s ON s.id=occ.html_document_id JOIN sec.filings f ON f.id=p.filing_id JOIN sec.companies issuer ON issuer.id=f.company_id
LEFT JOIN LATERAL (SELECT count(*) AS issue_count,array_agg(q.code ORDER BY q.issue_key) AS issue_codes
    FROM sec.source_quality_issues q JOIN sec.cell_source_fact_bindings binding ON binding.id=q.binding_id
    JOIN sec.source_processing_issues selected ON selected.issue_id=q.id AND selected.processing_set_id=p.id
    WHERE binding.fact_id=fact.id) issues ON true;
CREATE VIEW sec.analysis_ready_facts AS SELECT * FROM sec.source_fact_catalog WHERE validation_status='passed' AND quality_issue_count=0;
CREATE VIEW sec.source_report_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.source_validation_status,r.id AS report_id,r.filing_id,
    r.source_document_id,r.extraction_version,r.full_text,member.chunking_version,ds.sequence,ds.document_type,ds.is_primary,
    s.source_url,s.sha256 AS source_sha256
FROM sec.source_processing_sets p JOIN sec.source_processing_reports member ON member.processing_set_id=p.id
JOIN sec.reports r ON r.id=member.report_id JOIN sec.source_document_set_members ds ON ds.document_set_id=p.document_set_id AND ds.source_document_id=r.source_document_id
JOIN sec.source_documents s ON s.id=r.source_document_id;
CREATE VIEW sec.source_chunk_catalog AS
SELECT p.id AS processing_set_id,p.set_key,p.validation_status,p.source_validation_status,c.*,r.source_document_id,
    s.source_url,s.sha256 AS source_sha256
FROM sec.source_processing_sets p JOIN sec.source_processing_reports member ON member.processing_set_id=p.id
JOIN sec.chunks c ON c.report_id=member.report_id AND c.chunking_version=member.chunking_version
JOIN sec.reports r ON r.id=member.report_id JOIN sec.source_documents s ON s.id=r.source_document_id;
COMMENT ON VIEW sec.source_statement_cells IS 'V4 exact source cells, including visible review-required semantic conflicts. Source fidelity is distinct from semantic validity; original filing/source identities are retained.';
COMMENT ON VIEW sec.analysis_ready_statement_cells IS 'Explicit V4 selections with no observed conflicts under their declared checks. No preferred legacy selection is changed; this is not a claim of universal accounting validation.';
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
        GRANT SELECT ON sec.source_document_sets,sec.source_document_set_members,sec.fact_source_occurrences,
            sec.cell_source_fact_bindings,sec.source_quality_issues,sec.source_processing_sets,sec.source_processing_tables,
            sec.source_processing_reports,sec.source_processing_issues,sec.source_processing_overview,sec.source_statement_cells,
            sec.analysis_ready_statement_cells,sec.source_fact_catalog,sec.analysis_ready_facts,sec.source_report_catalog,sec.source_chunk_catalog TO sec_reader;
    END IF;
END $$;


CREATE FUNCTION sec.validate_v4_review_scope(target_table uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE issue record; group_ids jsonb; identifier text; group_index integer; allowed text[];
        cash_names text[]:=ARRAY['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
            'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations'];
BEGIN
    FOR issue IN SELECT q.*,c.column_period,c.column_index,c.row_index,c.row_heading,c.display_text,c.column_headings,c.source_locator,
        f.id AS fact_id,f.source_document_id AS xml_document_id,f.occurrence_key,f.concept_namespace,f.concept_name,
        f.period_kind,f.instant_date,f.is_nil,f.numeric_value,f.unit,f.dimensions,f.source_reference,
        t.statement_type,t.source_document_id AS html_document_id,t.structure,s.sha256 AS html_sha256,
        o.evidence->>'inline_xpath' AS original_inline_xpath,o.source_xpath AS numeric_source_xpath
        FROM sec.source_quality_issues q JOIN sec.cell_source_fact_bindings b ON b.id=q.binding_id
        JOIN sec.table_cells c ON c.id=b.cell_id JOIN sec.financial_facts f ON f.id=b.fact_id
        JOIN sec.financial_tables t ON t.id=b.table_id JOIN sec.source_documents s ON s.id=t.source_document_id
        JOIN sec.fact_source_occurrences o ON o.id=b.occurrence_id WHERE b.table_id=target_table LOOP
        IF issue.statement_type<>'cash_flow' OR issue.is_nil OR issue.numeric_value IS NULL OR issue.dimensions<>'[]'::jsonb
            OR issue.concept_namespace !~ '^https?://(fasb[.]org|xbrl[.]us)/us-gaap/[^/]+$'
            OR coalesce(issue.source_reference#>>'{entity,scheme}','') NOT IN ('http://www.sec.gov/CIK','https://www.sec.gov/CIK')
            OR coalesce(issue.source_reference#>>'{entity,identifier}','') !~ '^[0-9]{1,10}$'
            OR issue.unit->'denominator' IS DISTINCT FROM '[]'::jsonb OR jsonb_array_length(issue.unit->'numerator') IS DISTINCT FROM 1
            OR coalesce(issue.unit#>>'{numerator,0}','') !~ '^[{]http://www[.]xbrl[.]org/2003/iso4217[}][A-Z]{3}$'
            OR issue.evidence->>'source_correspondence' IS DISTINCT FROM 'exact_inline_xml'
            OR issue.evidence->>'html_document_id' IS DISTINCT FROM issue.html_document_id::text
            OR issue.evidence->>'html_sha256' IS DISTINCT FROM issue.html_sha256
            OR issue.evidence->>'xml_sha256' IS DISTINCT FROM issue.structure->>'xml_sha256'
            OR issue.evidence->>'cell_xpath' IS DISTINCT FROM issue.source_locator->>'xpath'
            OR issue.evidence->>'inline_xpath' IS DISTINCT FROM issue.original_inline_xpath
            OR issue.evidence->>'numeric_source_xpath' IS DISTINCT FROM issue.numeric_source_xpath
            OR issue.evidence->>'inline_id' IS DISTINCT FROM issue.occurrence_key
            OR issue.evidence->>'display_text' IS DISTINCT FROM issue.display_text
            OR issue.evidence->>'row_heading' IS DISTINCT FROM issue.row_heading
            OR issue.evidence->'column_headings' IS DISTINCT FROM issue.column_headings
            OR jsonb_typeof(issue.evidence#>'{core_coverage,flow_xml_ids}') IS DISTINCT FROM 'array'
            OR jsonb_array_length(issue.evidence#>'{core_coverage,flow_xml_ids}') IS DISTINCT FROM 3
            OR jsonb_typeof(issue.evidence#>'{core_coverage,closing_cash_xml_ids}') IS DISTINCT FROM 'array'
            OR jsonb_array_length(issue.evidence#>'{core_coverage,closing_cash_xml_ids}')=0 THEN
            RAISE EXCEPTION 'Review requires exact ancillary source evidence and a complete independent cash core' USING ERRCODE='23514';
        END IF;
        FOR group_index IN 0..3 LOOP
            group_ids:=CASE WHEN group_index=3 THEN issue.evidence#>'{core_coverage,closing_cash_xml_ids}'
                            ELSE issue.evidence#>'{core_coverage,flow_xml_ids}'->group_index END;
            allowed:=CASE group_index
                WHEN 0 THEN ARRAY['NetCashProvidedByUsedInOperatingActivities','NetCashProvidedByUsedInOperatingActivitiesContinuingOperations']
                WHEN 1 THEN ARRAY['NetCashProvidedByUsedInInvestingActivities','NetCashProvidedByUsedInInvestingActivitiesContinuingOperations']
                WHEN 2 THEN ARRAY['NetCashProvidedByUsedInFinancingActivities','NetCashProvidedByUsedInFinancingActivitiesContinuingOperations']
                ELSE cash_names END;
            IF jsonb_typeof(group_ids) IS DISTINCT FROM 'array' OR jsonb_array_length(group_ids)=0 THEN
                RAISE EXCEPTION 'Review cannot replace missing core statement facts' USING ERRCODE='23514';
            END IF;
            FOR identifier IN SELECT value FROM jsonb_array_elements_text(group_ids) LOOP
                IF identifier=issue.occurrence_key OR NOT EXISTS(
                    SELECT 1 FROM sec.cell_fact_links link JOIN sec.financial_facts core ON core.id=link.fact_id JOIN sec.table_cells cell ON cell.id=link.cell_id
                    WHERE link.table_id=target_table AND core.occurrence_key=identifier AND core.source_document_id=issue.xml_document_id
                    AND core.extraction_version='facts-v1' AND core.concept_namespace ~ '^https?://(fasb[.]org|xbrl[.]us)/us-gaap/[^/]+$'
                    AND core.concept_name=ANY(allowed) AND core.unit=issue.unit AND core.dimensions=issue.dimensions
                    AND core.source_reference->'entity'=issue.source_reference->'entity' AND NOT core.is_nil AND core.numeric_value IS NOT NULL
                    AND cell.column_period=issue.column_period AND cell.resolution_status='resolved'
                    AND ((group_index<3 AND core.period_kind='duration' AND core.period_start=(issue.column_period->>'startDate')::date
                          AND core.period_end=(issue.column_period->>'endDate')::date)
                         OR (group_index=3 AND core.period_kind='instant' AND core.instant_date=(issue.column_period->>'endDate')::date
                             AND cell.row_heading ~* '(end of (the )?(period|year|quarter)|ending balance|closing balance)'))) THEN
                    RAISE EXCEPTION 'Declared cash core must use exact accepted facts with the same source period/entity/currency' USING ERRCODE='23514';
                END IF;
            END LOOP;
        END LOOP;
        IF issue.code='instant_context_under_duration_header' AND (issue.instant_date IS DISTINCT FROM (issue.column_period->>'endDate')::date
            OR issue.concept_name=ANY(cash_names) OR issue.row_heading ~* '(beginning|opening|ending|closing|end of|start of)') THEN
            RAISE EXCEPTION 'Ancillary instant issue cannot reinterpret a cash endpoint' USING ERRCODE='23514';
        END IF;
        IF issue.code='opening_balance_date_not_prior_day' AND (
            NOT(issue.evidence#>'{core_coverage,closing_cash_xml_ids}' ? (issue.evidence#>>'{core_coverage,corresponding_closing_cash_xml_id}'))
            OR NOT EXISTS(SELECT 1 FROM sec.cell_fact_links link JOIN sec.financial_facts core ON core.id=link.fact_id
                JOIN sec.table_cells cell ON cell.id=link.cell_id WHERE link.table_id=target_table
                AND core.occurrence_key=issue.evidence#>>'{core_coverage,corresponding_closing_cash_xml_id}'
                AND core.concept_name=issue.concept_name AND cell.column_index=issue.column_index AND cell.row_index>issue.row_index)) THEN
            RAISE EXCEPTION 'Opening date issue requires a corresponding later accepted closing balance' USING ERRCODE='23514';
        END IF;
    END LOOP;
END; $$;


-- Version-gated managed-cell guards; V1/V2/V3 paths retain their prior checks.

ALTER TABLE sec.financial_tables DROP CONSTRAINT financial_tables_relational_cells_version_check;
ALTER TABLE sec.financial_tables ADD CONSTRAINT financial_tables_relational_cells_version_check CHECK(relational_cells_version IS NULL OR relational_cells_version IN(1,2,3));
ALTER TABLE sec.table_cells DROP CONSTRAINT table_cells_resolution_status_check;
ALTER TABLE sec.table_cells ADD CONSTRAINT table_cells_resolution_status_check CHECK(resolution_status IN('resolved','unresolved','non_fact','source_only','review_required'));
ALTER TABLE sec.table_cells ADD CONSTRAINT table_cells_review_data_check CHECK(resolution_status<>'review_required' OR kind='data');


CREATE OR REPLACE FUNCTION sec.guard_table_cell_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_table uuid;
    target_filing uuid;
    parent sec.financial_tables%ROWTYPE;
    source_cell jsonb;
    expected_kind text;
    expected_status text;
BEGIN
    target_table := CASE WHEN TG_OP = 'DELETE' THEN OLD.table_id ELSE NEW.table_id END;
    target_filing := CASE WHEN TG_OP = 'DELETE' THEN OLD.filing_id ELSE NEW.filing_id END;
    PERFORM sec.lock_processing_filing(target_filing);
    IF TG_OP = 'UPDATE' AND (NEW.table_id, NEW.filing_id) IS DISTINCT FROM (OLD.table_id, OLD.filing_id) THEN
        RAISE EXCEPTION 'Cell identity cannot move to another table or filing' USING ERRCODE = '23514';
    END IF;
    IF sec.table_scope_is_sealed(target_table) THEN
        IF TG_OP = 'INSERT' AND EXISTS (
            SELECT 1 FROM sec.table_cells c WHERE c.table_id = NEW.table_id AND c.cell_key = NEW.cell_key
            AND to_jsonb(c) - 'id' - 'created_at' = to_jsonb(NEW) - 'id' - 'created_at'
        ) THEN RETURN NEW;
        ELSIF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW;
        ELSE RAISE EXCEPTION 'Cells in a recorded processing set are immutable' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    SELECT * INTO parent FROM sec.financial_tables WHERE id = NEW.table_id AND filing_id = NEW.filing_id FOR SHARE;
    IF NOT FOUND OR ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version NOT IN ('tables-v3','tables-v4')) OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3') OR (parent.relational_cells_version = 3 AND parent.structure->>'schema_version' = '3' AND parent.extraction_version = 'tables-v4')) IS NOT TRUE THEN
        RAISE EXCEPTION 'Cell requires a schema-v1 relational table in the same filing' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key) <> 1 THEN
        RAISE EXCEPTION 'Cell key must identify exactly one retained source cell' USING ERRCODE = '23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = NEW.cell_key;
    expected_kind := CASE source_cell->>'classification' WHEN 'resolved' THEN 'data'
        WHEN 'unresolved' THEN 'data' WHEN 'source_only_marker' THEN 'data' WHEN 'source_conflict' THEN 'data' WHEN 'resolved_label' THEN 'label' ELSE source_cell->>'classification' END;
    expected_status := CASE source_cell->>'classification' WHEN 'resolved' THEN 'resolved'
        WHEN 'resolved_label' THEN 'resolved' WHEN 'unresolved' THEN 'unresolved' WHEN 'source_only_marker' THEN 'source_only' WHEN 'source_conflict' THEN 'review_required' ELSE 'non_fact' END;
    IF NEW.resolution_status = 'source_only' AND (parent.relational_cells_version NOT IN (2,3)
        OR NEW.kind <> 'data' OR NEW.display_text NOT IN ('-', '–', '—')
        OR source_cell ?| ARRAY['numeric_value', 'raw_value', 'is_nil']
        OR source_cell->'source_only_evidence'->>'verification_version' IS DISTINCT FROM 'source-marker-v1'
        OR source_cell->'source_only_evidence'->>'literal' IS DISTINCT FROM NEW.display_text
        OR source_cell->'source_only_evidence'->>'source_xpath' IS DISTINCT FROM source_cell->>'source_xpath'
        OR source_cell->'source_only_evidence'->>'html_sha256' IS DISTINCT FROM parent.structure->>'html_sha256'
        OR source_cell->'source_only_evidence'->>'binding_scope' IS DISTINCT FROM 'archived_dom_numeric_and_sec_hidden_bindings'
        OR source_cell->'source_only_evidence'->>'runtime_scope' IS DISTINCT FROM 'scripts_not_executed'
        OR jsonb_typeof(source_cell->'source_only_evidence'->'external_scripts') IS DISTINCT FROM 'array'
        OR source_cell->'source_only_evidence'->'direct_numeric_ids' IS DISTINCT FROM '[]'::jsonb
        OR source_cell->'source_only_evidence'->'hidden_binding_targets' IS DISTINCT FROM '[]'::jsonb) THEN
        RAISE EXCEPTION 'Source-only cells require explicit V3 absence evidence' USING ERRCODE = '23514';
    END IF;
    IF NEW.resolution_status <> 'source_only' AND source_cell ? 'source_only_evidence' THEN
        RAISE EXCEPTION 'Only source-only cells may retain absence evidence' USING ERRCODE = '23514';
    END IF;
    IF NEW.resolution_status='review_required' AND (parent.relational_cells_version<>3
        OR jsonb_typeof(source_cell->'source_fact_references') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'source_fact_references')<>1
        OR jsonb_typeof(source_cell->'quality_issues') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'quality_issues')=0
        OR jsonb_typeof(source_cell->'quality_issue_keys') IS DISTINCT FROM 'array'
        OR jsonb_array_length(source_cell->'quality_issue_keys')<>jsonb_array_length(source_cell->'quality_issues')) THEN
        RAISE EXCEPTION 'Review cells require explicit V4 source bindings and quality issues' USING ERRCODE='23514';
    END IF;
    IF NEW.resolution_status<>'review_required' AND (
        coalesce(jsonb_array_length(source_cell->'source_fact_references'),0)>0
        OR coalesce(jsonb_array_length(source_cell->'quality_issues'),0)>0
        OR coalesce(jsonb_array_length(source_cell->'quality_issue_keys'),0)>0) THEN
        RAISE EXCEPTION 'Only review-required cells may carry semantic conflict bindings' USING ERRCODE='23514';
    END IF;
    IF (NEW.row_index, NEW.column_index, NEW.row_span, NEW.column_span, NEW.kind, NEW.display_text,
        NEW.source_locator, NEW.resolution_status, NEW.unresolved_reason, NEW.row_heading,
        NEW.column_headings, NEW.column_period, NEW.is_total) IS DISTINCT FROM
       ((source_cell->>'row')::integer, (source_cell->>'column')::integer,
        (source_cell->>'rowspan')::integer, (source_cell->>'colspan')::integer, expected_kind,
        source_cell->>'display_text', jsonb_build_object('xpath', source_cell->>'source_xpath',
        'html_id', source_cell->'source_html_id'), expected_status, source_cell->>'unresolved_reason',
        source_cell->>'row_heading', source_cell->'column_headings', nullif(source_cell->'column_period', 'null'::jsonb),
        (source_cell->>'is_total')::boolean) THEN
        RAISE EXCEPTION 'Relational cell differs from retained table evidence' USING ERRCODE = '23514';
    END IF;
    IF (NEW.resolution_status = 'resolved' AND jsonb_array_length(source_cell->'fact_references') = 0)
        OR (NEW.resolution_status <> 'resolved' AND jsonb_array_length(source_cell->'fact_references') <> 0) THEN
        RAISE EXCEPTION 'Only resolved cells can have accepted fact references' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.table_cells c WHERE c.table_id = NEW.table_id AND c.id <> NEW.id
        AND c.cell_key <> NEW.cell_key
        AND int8range(c.row_index, c.row_index::bigint + c.row_span, '[)') && int8range(NEW.row_index, NEW.row_index::bigint + NEW.row_span, '[)')
        AND int8range(c.column_index, c.column_index::bigint + c.column_span, '[)') && int8range(NEW.column_index, NEW.column_index::bigint + NEW.column_span, '[)')) THEN
        RAISE EXCEPTION 'Relational cell spans overlap' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.guard_cell_fact_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_table uuid;
    target_filing uuid;
    cell sec.table_cells%ROWTYPE;
    parent sec.financial_tables%ROWTYPE;
    fact sec.financial_facts%ROWTYPE;
    expected jsonb;
    period jsonb;
BEGIN
    target_table := CASE WHEN TG_OP = 'DELETE' THEN OLD.table_id ELSE NEW.table_id END;
    target_filing := CASE WHEN TG_OP = 'DELETE' THEN OLD.filing_id ELSE NEW.filing_id END;
    PERFORM sec.lock_processing_filing(target_filing);
    IF TG_OP = 'UPDATE' AND (NEW.table_id, NEW.cell_id, NEW.filing_id, NEW.fact_id)
        IS DISTINCT FROM (OLD.table_id, OLD.cell_id, OLD.filing_id, OLD.fact_id) THEN
        RAISE EXCEPTION 'Fact-link identity cannot be reassigned' USING ERRCODE = '23514';
    END IF;
    IF sec.table_scope_is_sealed(target_table) THEN
        IF TG_OP = 'INSERT' AND EXISTS (SELECT 1 FROM sec.cell_fact_links l
            WHERE l.cell_id = NEW.cell_id AND l.fact_id = NEW.fact_id
            AND to_jsonb(l) - 'created_at' = to_jsonb(NEW) - 'created_at') THEN RETURN NEW;
        ELSIF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW;
        ELSE RAISE EXCEPTION 'Cell links in a recorded processing set are immutable' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    SELECT * INTO cell FROM sec.table_cells WHERE id = NEW.cell_id AND table_id = NEW.table_id AND filing_id = NEW.filing_id;
    IF NOT FOUND OR cell.resolution_status <> 'resolved' THEN
        RAISE EXCEPTION 'Accepted fact links require a resolved cell in the same filing' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO parent FROM sec.financial_tables WHERE id = cell.table_id FOR SHARE;
    SELECT c->'fact_references'->NEW.reference_order INTO expected
        FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id' = cell.cell_key;
    IF expected IS NULL OR NEW.evidence IS DISTINCT FROM expected OR NEW.link_method IS DISTINCT FROM expected->>'method'
        OR nullif(btrim(expected->>'inline_xpath'), '') IS NULL
        OR (NEW.link_method = 'same_row_semantic_repeat' AND nullif(btrim(expected->>'repeated_from_inline_xpath'), '') IS NULL) THEN
        RAISE EXCEPTION 'Fact-link evidence must match an accepted source reference' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO fact FROM sec.financial_facts WHERE id = NEW.fact_id AND filing_id = NEW.filing_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Fact belongs to another filing or does not exist' USING ERRCODE = '23503'; END IF;
    IF NEW.link_method = 'hidden_inline_id' AND (((parent.relational_cells_version=2 AND parent.extraction_version='tables-v3' AND parent.structure->>'schema_version'='2')
            OR (parent.relational_cells_version=3 AND parent.extraction_version='tables-v4' AND parent.structure->>'schema_version'='3')) IS NOT TRUE
        OR fact.is_nil IS NOT TRUE OR expected->'is_nil' IS DISTINCT FROM 'true'::jsonb
        OR expected->>'binding_property' IS DISTINCT FROM '-sec-ix-hidden'
        OR expected->>'binding_target_id' IS DISTINCT FROM expected->>'xml_id'
        OR nullif(btrim(expected->>'binding_xpath'), '') IS NULL
        OR nullif(btrim(expected->>'binding_style'), '') IS NULL
        OR nullif(btrim(expected->>'hidden_container_xpath'), '') IS NULL) THEN
        RAISE EXCEPTION 'Hidden fact links require an explicit V3 nil binding' USING ERRCODE = '23514';
    END IF;
    period := CASE fact.period_kind
        WHEN 'instant' THEN jsonb_build_object('instant', fact.instant_date::text)
        WHEN 'duration' THEN jsonb_build_object('startDate', fact.period_start::text, 'endDate', fact.period_end::text)
        ELSE jsonb_build_object('forever', '') END;
    IF (fact.source_document_id::text, fact.extraction_version, fact.filing_id::text) IS DISTINCT FROM
        (expected#>>'{scope,xml_document_id}', expected#>>'{scope,facts_version}', expected#>>'{scope,filing_id}')
        OR expected->'scope' IS DISTINCT FROM parent.structure->'fact_scope'
        OR (fact.concept_namespace || '}' || fact.concept_name) IS DISTINCT FROM substring(expected->>'concept' FROM 2)
        OR fact.raw_value IS DISTINCT FROM (CASE WHEN fact.is_nil THEN NULL ELSE expected->>'raw_value' END)
        OR fact.unit IS DISTINCT FROM nullif(expected->'unit', 'null'::jsonb)
        OR period IS DISTINCT FROM expected->'period'
        OR fact.dimensions IS DISTINCT FROM expected->'dimensions'
        OR fact.is_nil IS DISTINCT FROM (expected->>'is_nil')::boolean
        OR fact.numeric_value IS DISTINCT FROM (CASE WHEN fact.is_nil OR fact.unit IS NULL THEN NULL ELSE (expected->>'raw_value')::numeric END)
        OR (fact.source_reference ? 'context_ref' AND fact.source_reference->>'context_ref' IS DISTINCT FROM expected->>'context_ref')
        OR (fact.source_reference ? 'unit_ref' AND fact.source_reference->>'unit_ref' IS DISTINCT FROM expected->>'unit_ref')
        OR (expected->>'xml_id' = fact.occurrence_key
            OR expected->>'xml_id' = ANY(ARRAY[fact.source_reference->>'element_id', fact.source_reference->>'xml_id',
                                              fact.source_reference->>'id', fact.source_reference->>'fact_id'])) IS NOT TRUE THEN
        RAISE EXCEPTION 'Stored fact differs from verified cell evidence or source/version scope' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.guard_cell_source_observation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent sec.financial_tables%ROWTYPE; cell sec.table_cells%ROWTYPE; source_cell jsonb; source_hash text;
BEGIN
    PERFORM sec.lock_processing_filing(CASE WHEN TG_OP='DELETE' THEN OLD.filing_id ELSE NEW.filing_id END);
    IF sec.table_scope_is_sealed(CASE WHEN TG_OP='DELETE' THEN OLD.table_id ELSE NEW.table_id END) THEN
        IF TG_OP='INSERT' AND EXISTS (SELECT 1 FROM sec.cell_source_observations o WHERE o.cell_id=NEW.cell_id
            AND to_jsonb(o)-'id'-'created_at'=to_jsonb(NEW)-'id'-'created_at') THEN RETURN NEW;
        ELSIF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW;
        ELSE RAISE EXCEPTION 'Source observations in a recorded processing set are immutable' USING ERRCODE='23514'; END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF TG_OP='UPDATE' AND (NEW.filing_id,NEW.table_id,NEW.cell_id) IS DISTINCT FROM (OLD.filing_id,OLD.table_id,OLD.cell_id) THEN
        RAISE EXCEPTION 'Source observations cannot move between cells' USING ERRCODE='23514';
    END IF;
    SELECT * INTO cell FROM sec.table_cells WHERE id=NEW.cell_id AND filing_id=NEW.filing_id AND table_id=NEW.table_id;
    IF NOT FOUND OR cell.kind <> 'data' OR cell.resolution_status <> 'source_only' THEN
        RAISE EXCEPTION 'Source observation requires a source-only financial cell' USING ERRCODE='23514';
    END IF;
    SELECT * INTO parent FROM sec.financial_tables WHERE id=NEW.table_id AND filing_id=NEW.filing_id FOR SHARE;
    IF NOT FOUND OR ((parent.relational_cells_version=2 AND parent.structure->>'schema_version'='2' AND parent.extraction_version='tables-v3')
        OR (parent.relational_cells_version=3 AND parent.structure->>'schema_version'='3' AND parent.extraction_version='tables-v4')) IS NOT TRUE OR parent.source_document_id <> NEW.source_document_id THEN
        RAISE EXCEPTION 'Source observation requires the exact V3 HTML table scope' USING ERRCODE='23514';
    END IF;
    SELECT c INTO source_cell FROM sec.table_structure_cells(parent.structure) c WHERE c->>'cell_id'=cell.cell_key;
    SELECT sha256 INTO source_hash FROM sec.source_documents WHERE id=NEW.source_document_id AND filing_id=NEW.filing_id AND role='main_inline_xbrl_filing';
    IF NOT FOUND OR NEW.source_sha256 IS DISTINCT FROM source_hash
        OR NEW.source_sha256 IS DISTINCT FROM parent.structure->>'html_sha256'
        OR NEW.evidence IS DISTINCT FROM source_cell->'source_only_evidence'
        OR NEW.verification_version IS DISTINCT FROM NEW.evidence->>'verification_version'
        OR NEW.evidence->>'literal' IS DISTINCT FROM cell.display_text
        OR NEW.evidence->>'source_xpath' IS DISTINCT FROM cell.source_locator->>'xpath' THEN
        RAISE EXCEPTION 'Source observation differs from retained cell/source evidence' USING ERRCODE='23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.cell_fact_links WHERE cell_id=NEW.cell_id) THEN
        RAISE EXCEPTION 'Source-only observations cannot have numeric fact links' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.validate_table_cell_consistency(target_table uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE parent sec.financial_tables%ROWTYPE;
BEGIN
    SELECT * INTO parent FROM sec.financial_tables WHERE id = target_table;
    IF NOT FOUND OR parent.relational_cells_version IS NULL THEN RETURN; END IF;
    IF ((parent.relational_cells_version = 1 AND parent.structure->>'schema_version' = '1' AND parent.extraction_version NOT IN ('tables-v3','tables-v4'))
        OR (parent.relational_cells_version = 2 AND parent.structure->>'schema_version' = '2' AND parent.extraction_version = 'tables-v3') OR (parent.relational_cells_version = 3 AND parent.structure->>'schema_version' = '3' AND parent.extraction_version = 'tables-v4')) IS NOT TRUE
        OR jsonb_typeof(parent.structure->'rows') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Managed cells require the schema-v1 table layout contract' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM sec.table_cells WHERE table_id = target_table) <>
       (SELECT count(*) FROM sec.table_structure_cells(parent.structure)) THEN
        RAISE EXCEPTION 'Relational cells must cover every retained anchor cell' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.table_cells c
        JOIN LATERAL (SELECT v FROM sec.table_structure_cells(parent.structure) v WHERE v->>'cell_id' = c.cell_key) source ON true
        WHERE c.table_id = target_table AND
        (SELECT count(*) FROM sec.cell_fact_links l WHERE l.cell_id = c.id) <> jsonb_array_length(source.v->'fact_references')) THEN
        RAISE EXCEPTION 'Cell links must cover exactly the accepted source references' USING ERRCODE = '23514';
    END IF;
    IF EXISTS ((SELECT fact_id FROM sec.financial_table_facts WHERE table_id = target_table
                EXCEPT SELECT fact_id FROM sec.cell_fact_links WHERE table_id = target_table)
               UNION ALL
               (SELECT fact_id FROM sec.cell_fact_links WHERE table_id = target_table
                EXCEPT SELECT fact_id FROM sec.financial_table_facts WHERE table_id = target_table)) THEN
        RAISE EXCEPTION 'Legacy table membership must equal accepted cell links' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.table_cells c LEFT JOIN sec.cell_source_observations o ON o.cell_id=c.id
        WHERE c.table_id=target_table AND ((c.resolution_status='source_only') IS DISTINCT FROM (o.cell_id IS NOT NULL))) THEN
        RAISE EXCEPTION 'Exactly source-only cells require retained source observations' USING ERRCODE = '23514';
    END IF;
    IF parent.relational_cells_version=3 THEN
        PERFORM sec.validate_v4_review_scope(target_table);
        IF EXISTS(SELECT 1 FROM sec.table_cells c JOIN LATERAL
            (SELECT v FROM sec.table_structure_cells(parent.structure) v WHERE v->>'cell_id'=c.cell_key) source ON true
            WHERE c.table_id=target_table AND (
                (SELECT count(*) FROM sec.cell_source_fact_bindings b WHERE b.cell_id=c.id)<>
                    coalesce(jsonb_array_length(source.v->'source_fact_references'),0)
                OR (c.resolution_status='review_required') IS DISTINCT FROM
                    EXISTS(SELECT 1 FROM sec.cell_source_fact_bindings b WHERE b.cell_id=c.id)
                OR coalesce((SELECT jsonb_agg(q.issue_key ORDER BY q.issue_key) FROM sec.source_quality_issues q
                    JOIN sec.cell_source_fact_bindings b ON b.id=q.binding_id WHERE b.cell_id=c.id),'[]'::jsonb)
                    IS DISTINCT FROM coalesce((SELECT jsonb_agg(value ORDER BY value) FROM jsonb_array_elements_text(source.v->'quality_issue_keys')),'[]'::jsonb))) THEN
            RAISE EXCEPTION 'V4 source bindings/issues must cover exactly retained review cells' USING ERRCODE='23514';
        END IF;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION sec.processing_artifact_is_sealed(artifact_table text, artifact jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF sec.v4_processing_artifact_is_sealed(artifact_table,artifact) THEN RETURN true; END IF;
    CASE artifact_table
    WHEN 'financial_facts' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets
            WHERE fact_source_document_id=(artifact->>'source_document_id')::uuid
              AND fact_extraction_version=artifact->>'extraction_version');
    WHEN 'financial_tables' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets
            WHERE table_source_document_id=(artifact->>'source_document_id')::uuid
              AND table_extraction_version=artifact->>'extraction_version');
    WHEN 'reports' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets AS p JOIN sec.reports AS r ON r.id=p.report_id
            WHERE r.source_document_id=(artifact->>'source_document_id')::uuid
              AND r.extraction_version=artifact->>'extraction_version');
    WHEN 'report_sections' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets WHERE report_id=(artifact->>'report_id')::uuid);
    WHEN 'chunks' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets
            WHERE report_id=(artifact->>'report_id')::uuid AND chunking_version=artifact->>'chunking_version');
    WHEN 'financial_table_facts' THEN
        RETURN sec.table_scope_is_sealed((artifact->>'table_id')::uuid);
    WHEN 'source_documents' THEN
        RETURN EXISTS (SELECT 1 FROM sec.filing_processing_sets AS p
            LEFT JOIN sec.reports AS r ON r.id=p.report_id
            WHERE (artifact->>'id')::uuid IN (p.fact_source_document_id,p.table_source_document_id,r.source_document_id));
    ELSE RAISE EXCEPTION 'Unknown processing artifact table: %', artifact_table;
    END CASE;
END;
$$;

CREATE OR REPLACE FUNCTION sec.table_scope_is_sealed(target_table_id uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM sec.financial_tables AS t
        JOIN sec.filing_processing_sets AS p
          ON p.table_source_document_id = t.source_document_id
         AND p.table_extraction_version = t.extraction_version
        WHERE t.id = target_table_id
    ) OR EXISTS(SELECT 1 FROM sec.financial_tables t WHERE t.id=target_table_id AND sec.v4_processing_artifact_is_sealed('financial_tables',to_jsonb(t)));
$$;

