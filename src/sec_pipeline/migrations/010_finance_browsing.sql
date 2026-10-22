-- Standardized, source-linked financial reads. No source rows or preferences change.
CREATE SCHEMA finance;
COMMENT ON SCHEMA finance IS 'Financial browsing views; sec retains immutable source evidence. Values are as filed, not a restatement-adjusted time series.';
REVOKE ALL ON SCHEMA finance FROM PUBLIC;

CREATE VIEW finance.company AS
SELECT id AS company_id, cik, ticker, name AS company_name FROM sec.companies;

CREATE VIEW finance.filing_version AS
SELECT 'legacy'::text AS processing_family, p.id AS processing_set_id, p.filing_id,
       p.set_key, p.validation_status, NULL::text AS source_validation_status,
       p.coverage, p.created_at, pref.processing_set_id IS NOT NULL AS is_preferred,
       p.fact_source_document_id, p.fact_extraction_version
FROM sec.filing_processing_sets p
LEFT JOIN sec.preferred_processing_sets pref ON pref.filing_id=p.filing_id AND pref.processing_set_id=p.id
UNION ALL
SELECT 'source', p.id, p.filing_id, p.set_key, p.validation_status, p.source_validation_status,
       p.coverage, p.created_at, false, ds.xml_document_id, p.versions->>'facts'
FROM sec.source_processing_sets p JOIN sec.source_document_sets ds ON ds.id=p.document_set_id;

CREATE VIEW finance.filing AS
SELECT f.id AS filing_id, c.company_id, c.cik, c.ticker, c.company_name,
       f.company_name_as_filed, f.accession_number, f.form, f.filing_date,
       f.reporting_period_end, f.amendment_of_id, f.source_url,
       v.processing_family, v.processing_set_id, v.set_key,
       v.validation_status, v.source_validation_status, v.coverage,
       v.fact_source_document_id, v.fact_extraction_version,
       CASE WHEN v.is_preferred THEN 'explicit_legacy_preference'
            WHEN v.processing_family='source' THEN 'newest_source_version'
            WHEN v.processing_family='legacy' THEN 'newest_passed_legacy_version'
            ELSE 'no_processed_content' END AS selection_reason
FROM sec.filings f JOIN finance.company c ON c.company_id=f.company_id
LEFT JOIN LATERAL (
    SELECT * FROM finance.filing_version v WHERE v.filing_id=f.id
      AND (v.processing_family='source' OR v.validation_status='passed')
    ORDER BY v.is_preferred DESC, (v.processing_family='source') DESC,
             (v.fact_source_document_id IS NOT NULL) DESC, v.created_at DESC, v.processing_set_id DESC
    LIMIT 1
) v ON true;
COMMENT ON VIEW finance.filing IS 'One convenience selection per filing: explicit legacy preference, otherwise newest source set (including review), otherwise newest passed legacy set with facts. No stored preference changes. Filtering a saved family/set ID detects default drift; historical replay uses the explicit sec source scopes.';

CREATE VIEW finance.statement AS
SELECT f.filing_id, f.company_id, f.cik, f.ticker, f.processing_family, f.processing_set_id,
       t.id AS statement_id, t.statement_type, t.table_key, t.source_document_id,
       t.extraction_version, t.readable_text, s.source_url, s.sha256 AS source_sha256,
       f.validation_status,f.source_validation_status,f.coverage
FROM finance.filing f JOIN sec.filing_processing_sets p ON f.processing_family='legacy' AND p.id=f.processing_set_id
JOIN sec.financial_tables t ON t.filing_id=f.filing_id AND t.source_document_id=p.table_source_document_id AND t.extraction_version=p.table_extraction_version
JOIN sec.source_documents s ON s.id=t.source_document_id
UNION ALL
SELECT f.filing_id,f.company_id,f.cik,f.ticker,f.processing_family,f.processing_set_id,
       t.id,t.statement_type,t.table_key,t.source_document_id,t.extraction_version,t.readable_text,s.source_url,s.sha256,
       f.validation_status,f.source_validation_status,f.coverage
FROM finance.filing f JOIN sec.source_processing_tables m ON f.processing_family='source' AND m.processing_set_id=f.processing_set_id
JOIN sec.financial_tables t ON t.id=m.table_id JOIN sec.source_documents s ON s.id=t.source_document_id;

CREATE VIEW finance.filing_report AS
SELECT f.filing_id, f.processing_family, f.processing_set_id, r.id AS report_id,
       r.source_document_id, r.extraction_version, p.chunking_version, r.full_text,
       s.source_url, s.sha256 AS source_sha256,f.validation_status,f.source_validation_status,f.coverage
FROM finance.filing f JOIN sec.filing_processing_sets p ON f.processing_family='legacy' AND p.id=f.processing_set_id
JOIN sec.reports r ON r.id=p.report_id JOIN sec.source_documents s ON s.id=r.source_document_id
UNION ALL
SELECT f.filing_id,f.processing_family,f.processing_set_id,r.id,r.source_document_id,r.extraction_version,
       m.chunking_version,r.full_text,s.source_url,s.sha256,f.validation_status,f.source_validation_status,f.coverage
FROM finance.filing f JOIN sec.source_processing_reports m ON f.processing_family='source' AND m.processing_set_id=f.processing_set_id
JOIN sec.reports r ON r.id=m.report_id JOIN sec.source_documents s ON s.id=r.source_document_id;

CREATE VIEW finance.filing_section AS
SELECT r.filing_id,r.processing_family,r.processing_set_id,r.report_id,r.source_document_id,
       s.id AS section_id,s.section_key,s.section_type,s.original_title,s.part,s.item,s.section_order,
       s.offset_start,s.offset_end,substring(r.full_text FROM s.offset_start+1 FOR s.offset_end-s.offset_start) AS content,
       s.source_locator,s.validation_status AS section_validation_status,r.extraction_version,r.source_url,r.source_sha256,
       r.validation_status,r.source_validation_status,r.coverage
FROM finance.filing_report r JOIN sec.report_sections s ON s.report_id=r.report_id;

CREATE VIEW finance.filing_chunk AS
SELECT r.filing_id,r.processing_family,r.processing_set_id,r.report_id,r.source_document_id,
       c.id AS chunk_id,c.section_id,c.chunk_index,c.section AS section_key,c.text AS content,
       c.offset_start AS char_start,c.offset_end AS char_end,c.chunking_version,
       r.extraction_version AS report_extraction_version,c.metadata,r.source_url,r.source_sha256,
       r.validation_status,r.source_validation_status,r.coverage
FROM finance.filing_report r JOIN sec.chunks c ON c.report_id=r.report_id AND c.chunking_version=r.chunking_version;
COMMENT ON VIEW finance.filing_chunk IS 'Exact cleaned-report slices; char_start/char_end are zero-based half-open Unicode character offsets, not HTML offsets. Token counts and embeddings are model-specific downstream artifacts.';

CREATE VIEW finance.metric_definition AS
SELECT *, 'financial-metrics-v1'::text AS mapping_version FROM (VALUES
 ('revenue','income_statement','duration','currency',ARRAY['Revenues','SalesRevenueNet','RevenueFromContractWithCustomerExcludingAssessedTax'],'reported','Reported revenue; contract revenue requires a linked top-line statement cell. Conflicting bases are withheld.'),
 ('cost_of_revenue','income_statement','duration','currency',ARRAY['CostOfRevenue','CostOfGoodsAndServicesSold'],'reported','Reported cost of revenue/goods and services; concept basis retained, conflicting values withheld.'),
 ('operating_income','income_statement','duration','currency',ARRAY['OperatingIncomeLoss'],'reported','Operating income or loss.'),
 ('net_income','income_statement','duration','currency',ARRAY['NetIncomeLoss'],'reported','Net income attributable to the parent; excludes income attributable to noncontrolling interests.'),
 ('eps_diluted','income_statement','duration','currency_per_share',ARRAY['EarningsPerShareDiluted'],'reported','Diluted earnings per share; never sum quarters or subtract year-to-date EPS.'),
 ('cash_and_equivalents','balance_sheet','instant','currency',ARRAY['CashAndCashEquivalentsAtCarryingValue'],'reported','Cash and cash equivalents, without adding restricted cash.'),
 ('total_assets','balance_sheet','instant','currency',ARRAY['Assets'],'reported','Total assets.'),
 ('total_liabilities','balance_sheet','instant','currency',ARRAY['Liabilities'],'reported','Total liabilities; not liabilities plus equity.'),
 ('shareholders_equity','balance_sheet','instant','currency',ARRAY['StockholdersEquity'],'reported','Equity attributable to the parent, excluding noncontrolling interests.'),
 ('short_term_borrowings','balance_sheet','instant','currency',ARRAY['ShortTermBorrowings'],'reported','Borrowings originally due within one year or the operating cycle.'),
 ('long_term_debt_current','balance_sheet','instant','currency',ARRAY['LongTermDebtCurrent'],'reported','Current portion of long-term debt, excluding lease obligations.'),
 ('long_term_debt_noncurrent','balance_sheet','instant','currency',ARRAY['LongTermDebtNoncurrent'],'reported','Noncurrent long-term debt, excluding lease obligations.'),
 ('total_debt_excluding_leases','balance_sheet','instant','currency',ARRAY[]::text[],'derived','Short-term borrowings + current long-term debt + noncurrent long-term debt; requires all three reported components, including explicit zeros.'),
 ('operating_cash_flow','cash_flow_statement','duration','currency',ARRAY['NetCashProvidedByUsedInOperatingActivities'],'reported','Net cash provided by or used in operating activities.'),
 ('capital_expenditures','cash_flow_statement','duration','currency',ARRAY['PaymentsToAcquirePropertyPlantAndEquipment'],'reported','Cash payments for property, plant and equipment; positive cash outflow, not every possible definition of capex.'),
 ('free_cash_flow','cash_flow_statement','duration','currency',ARRAY[]::text[],'derived','Operating cash flow minus cash PP&E payments for the exact same duration and currency; no missing-value substitution.'),
 ('dividends_paid','cash_flow_statement','duration','currency',ARRAY['PaymentsOfDividends'],'reported','Total cash dividends/distributions; common-only dividends are not substituted.')
) d(metric_key,statement_type,period_kind,unit_kind,concepts,method,definition);

CREATE VIEW finance.metric_evidence AS
WITH candidates AS (
 SELECT f.filing_id,f.company_id,f.cik,f.ticker,f.processing_family,f.processing_set_id,f.validation_status,
        d.metric_key,d.statement_type,d.period_kind AS expected_period_kind,d.unit_kind,d.mapping_version,
        a.id AS fact_id,a.source_document_id,a.extraction_version,a.occurrence_key,a.concept_name,a.concept_namespace,
        a.numeric_value,a.raw_value,a.is_nil,a.period_kind,a.period_start,
        coalesce(a.period_end,a.instant_date) AS period_end,a.instant_date,a.unit,a.dimensions,a.source_reference,
        CASE WHEN a.period_kind='instant' THEN 'instant:'||to_char(a.instant_date,'YYYY-MM-DD')
             WHEN a.period_kind='duration' THEN 'duration:'||to_char(a.period_start,'YYYY-MM-DD')||':'||to_char(a.period_end,'YYYY-MM-DD') END AS period_id,
        substring(a.unit#>>'{numerator,0}' FROM '^\{https?://www[.]xbrl[.]org/2003/iso4217\}([A-Z]{3})$') AS currency,
        s.source_url,s.sha256 AS source_sha256,
        CASE WHEN a.concept_name <> 'RevenueFromContractWithCustomerExcludingAssessedTax' THEN true ELSE EXISTS (
          SELECT 1 FROM finance.statement st JOIN sec.table_cells tc ON tc.table_id=st.statement_id
          JOIN sec.cell_fact_links link ON link.cell_id=tc.id AND link.fact_id=a.id
          WHERE st.filing_id=f.filing_id AND st.statement_type='income_statement'
            AND lower(btrim(regexp_replace(tc.row_heading,'\s+',' ','g'))) IN
                ('revenue','revenues','total revenue','total revenues','net sales','net revenue','net revenues','sales')
        ) END AS top_line_verified
 FROM finance.filing f JOIN sec.financial_facts a
   ON a.filing_id=f.filing_id AND a.source_document_id=f.fact_source_document_id AND a.extraction_version=f.fact_extraction_version
 JOIN finance.metric_definition d ON a.concept_name=ANY(d.concepts)
 JOIN sec.source_documents s ON s.id=a.source_document_id
 WHERE a.concept_namespace ~ '^https?://(fasb[.]org|xbrl[.]us)/us-gaap/'
), checked AS (
 SELECT *, CASE
   WHEN dimensions <> '[]'::jsonb THEN 'dimensional_fact'
   WHEN source_reference#>>'{entity,scheme}' NOT IN ('http://www.sec.gov/CIK','https://www.sec.gov/CIK')
     OR source_reference#>>'{entity,scheme}' IS NULL
     OR coalesce(source_reference#>>'{entity,identifier}','') !~ '^[0-9]{1,10}$'
     OR lpad(source_reference#>>'{entity,identifier}',10,'0') IS DISTINCT FROM cik THEN 'different_or_unknown_issuer'
   WHEN period_kind IS DISTINCT FROM expected_period_kind OR period_id IS NULL THEN 'wrong_period_kind'
   WHEN currency IS NULL OR CASE WHEN jsonb_typeof(unit->'numerator')='array' THEN jsonb_array_length(unit->'numerator') ELSE 0 END <> 1
     OR (unit_kind='currency' AND unit->'denominator' IS DISTINCT FROM '[]'::jsonb)
     OR (unit_kind='currency_per_share' AND (CASE WHEN jsonb_typeof(unit->'denominator')='array' THEN jsonb_array_length(unit->'denominator') ELSE 0 END<>1
       OR coalesce(unit#>>'{denominator,0}','') !~ '^\{https?://www[.]xbrl[.]org/2003/instance\}shares$')) THEN 'unsupported_unit'
   WHEN NOT top_line_verified THEN 'unverified_total_revenue'
   WHEN is_nil THEN 'nil_fact'
   WHEN numeric_value IS NULL THEN 'non_numeric_fact'
   WHEN metric_key IN ('capital_expenditures','dividends_paid') AND numeric_value < 0 THEN 'unexpected_outflow_sign'
   ELSE NULL END AS exclusion_reason
 FROM candidates
)
SELECT *, exclusion_reason IS NULL AS eligible FROM checked;
COMMENT ON VIEW finance.metric_evidence IS 'Mapped original facts for the selected filing version, with exclusion reasons and exact source IDs. Identical duplicates are not summed. Company extensions and dimensioned facts are not guessed into consolidated metrics.';

CREATE VIEW finance.period AS
SELECT DISTINCT period_id,period_kind,period_start,period_end,
       CASE WHEN period_kind='duration' THEN period_end-period_start+1 END AS duration_days
FROM finance.metric_evidence WHERE eligible OR exclusion_reason='nil_fact';
COMMENT ON VIEW finance.period IS 'Actual observed instant/duration periods for supported metrics. No inferred fiscal year/quarter, calendar-quarter assignment, or synthetic stand-alone quarter.';

CREATE VIEW finance.metric_value AS
SELECT f.company_id,f.cik,f.ticker,f.company_name,f.filing_id,f.accession_number,f.form,f.filing_date,
       f.reporting_period_end,f.amendment_of_id,f.processing_family,f.processing_set_id,f.selection_reason,
       f.validation_status,f.coverage,c.period_id,c.period_kind,c.period_start,c.period_end,c.duration_days,
       c.currency,c.metric_key,c.statement_type,c.value,c.status,c.fact_ids,c.concept_names,c.mapping_version
FROM finance.filing f CROSS JOIN LATERAL (
WITH evidence AS MATERIALIZED (
 SELECT e.* FROM finance.metric_evidence e WHERE e.filing_id=f.filing_id
), periods AS (
 SELECT DISTINCT period_id,period_kind,period_start,period_end,
        CASE WHEN period_kind='duration' THEN period_end-period_start+1 END AS duration_days
 FROM evidence WHERE eligible OR exclusion_reason='nil_fact'
), available AS (
 SELECT DISTINCT filing_id,period_id,currency FROM evidence WHERE eligible OR exclusion_reason='nil_fact'
), values_by_metric AS (
 SELECT filing_id,period_id,currency,metric_key,count(DISTINCT numeric_value) AS distinct_values,
        min(numeric_value) AS candidate_value,array_agg(DISTINCT fact_id ORDER BY fact_id) AS fact_ids,
        array_agg(DISTINCT concept_name ORDER BY concept_name) AS concept_names
 FROM evidence WHERE eligible GROUP BY filing_id,period_id,currency,metric_key
), direct AS (
 SELECT a.filing_id,a.period_id,a.currency,d.metric_key,d.statement_type,d.mapping_version,
        CASE WHEN f.validation_status='passed' AND v.distinct_values=1 THEN v.candidate_value END AS value,
        CASE WHEN f.validation_status<>'passed' THEN 'review_required'
             WHEN v.distinct_values>1 THEN 'conflict' WHEN v.distinct_values=1 THEN 'reported' ELSE 'missing' END AS status,
        coalesce(v.fact_ids,ARRAY[]::uuid[]) AS fact_ids,coalesce(v.concept_names,ARRAY[]::text[]) AS concept_names
 FROM available a JOIN periods p ON p.period_id=a.period_id
 JOIN finance.metric_definition d ON d.period_kind=p.period_kind AND d.method='reported'
 LEFT JOIN values_by_metric v ON v.filing_id=a.filing_id AND v.period_id=a.period_id AND v.currency=a.currency AND v.metric_key=d.metric_key
), derived_inputs AS (
 SELECT d.filing_id,d.period_id,d.currency,def.metric_key,def.statement_type,def.mapping_version,
        bool_and(d.status='reported') AS inputs_reported,bool_or(d.status='review_required') AS needs_review,
        bool_or(d.status='conflict') AS has_conflict,
        sum(CASE WHEN def.metric_key='free_cash_flow' AND d.metric_key='capital_expenditures' THEN -d.value ELSE d.value END) AS computed_value,
        array_agg(d.metric_key) AS input_metrics,
        jsonb_agg(to_jsonb(d.fact_ids)) AS input_fact_ids,
        jsonb_agg(to_jsonb(d.concept_names)) AS input_concepts
 FROM direct d JOIN finance.metric_definition def ON def.method='derived'
  AND ((def.metric_key='free_cash_flow' AND d.metric_key IN ('operating_cash_flow','capital_expenditures'))
    OR (def.metric_key='total_debt_excluding_leases' AND d.metric_key IN ('short_term_borrowings','long_term_debt_current','long_term_debt_noncurrent')))
 GROUP BY d.filing_id,d.period_id,d.currency,def.metric_key,def.statement_type,def.mapping_version
), combined AS (
 SELECT * FROM direct
 UNION ALL
 SELECT filing_id,period_id,currency,metric_key,statement_type,mapping_version,
        CASE WHEN inputs_reported AND cardinality(input_metrics)=CASE WHEN metric_key='free_cash_flow' THEN 2 ELSE 3 END THEN computed_value END,
        CASE WHEN needs_review THEN 'review_required' WHEN has_conflict THEN 'conflict'
             WHEN inputs_reported AND cardinality(input_metrics)=CASE WHEN metric_key='free_cash_flow' THEN 2 ELSE 3 END THEN 'derived' ELSE 'missing' END,
        ARRAY(SELECT DISTINCT x::uuid FROM jsonb_array_elements(input_fact_ids) a CROSS JOIN LATERAL jsonb_array_elements_text(a) x ORDER BY x::uuid),
        ARRAY(SELECT DISTINCT x FROM jsonb_array_elements(input_concepts) a CROSS JOIN LATERAL jsonb_array_elements_text(a) x ORDER BY x)
 FROM derived_inputs
)
SELECT combined.*,p.period_kind,p.period_start,p.period_end,p.duration_days
FROM combined JOIN periods p USING (period_id)
) c;
COMMENT ON VIEW finance.metric_value IS 'One metric per filing/exact period/currency in its convenience-selected version. Value is NULL for missing, conflicting or review-required evidence. Amounts retain original XBRL base units. Derived metrics require every input; not a latest-restated company time series.';

CREATE VIEW finance.income_statement AS
SELECT company_id,cik,ticker,company_name,filing_id,accession_number,form,filing_date,reporting_period_end,
       processing_family,processing_set_id,selection_reason,validation_status,period_id,period_start,period_end,duration_days,currency,
       max(value) FILTER (WHERE metric_key='revenue') AS revenue,
       max(value) FILTER (WHERE metric_key='cost_of_revenue') AS cost_of_revenue,
       max(value) FILTER (WHERE metric_key='operating_income') AS operating_income,
       max(value) FILTER (WHERE metric_key='net_income') AS net_income,
       max(value) FILTER (WHERE metric_key='eps_diluted') AS eps_diluted,
       jsonb_object_agg(metric_key,status) AS metric_status
FROM finance.metric_value WHERE statement_type='income_statement'
GROUP BY company_id,cik,ticker,company_name,filing_id,accession_number,form,filing_date,reporting_period_end,
         processing_family,processing_set_id,selection_reason,validation_status,period_id,period_start,period_end,duration_days,currency;

CREATE VIEW finance.balance_sheet AS
SELECT company_id,cik,ticker,company_name,filing_id,accession_number,form,filing_date,reporting_period_end,
       processing_family,processing_set_id,selection_reason,validation_status,period_id,period_end,currency,
       max(value) FILTER (WHERE metric_key='cash_and_equivalents') AS cash_and_equivalents,
       max(value) FILTER (WHERE metric_key='total_assets') AS total_assets,
       max(value) FILTER (WHERE metric_key='total_liabilities') AS total_liabilities,
       max(value) FILTER (WHERE metric_key='shareholders_equity') AS shareholders_equity,
       max(value) FILTER (WHERE metric_key='short_term_borrowings') AS short_term_borrowings,
       max(value) FILTER (WHERE metric_key='long_term_debt_current') AS long_term_debt_current,
       max(value) FILTER (WHERE metric_key='long_term_debt_noncurrent') AS long_term_debt_noncurrent,
       max(value) FILTER (WHERE metric_key='total_debt_excluding_leases') AS total_debt_excluding_leases,
       jsonb_object_agg(metric_key,status) AS metric_status
FROM finance.metric_value WHERE statement_type='balance_sheet'
GROUP BY company_id,cik,ticker,company_name,filing_id,accession_number,form,filing_date,reporting_period_end,
         processing_family,processing_set_id,selection_reason,validation_status,period_id,period_end,currency;

CREATE VIEW finance.cash_flow_statement AS
SELECT company_id,cik,ticker,company_name,filing_id,accession_number,form,filing_date,reporting_period_end,
       processing_family,processing_set_id,selection_reason,validation_status,period_id,period_start,period_end,duration_days,currency,
       max(value) FILTER (WHERE metric_key='operating_cash_flow') AS operating_cash_flow,
       max(value) FILTER (WHERE metric_key='capital_expenditures') AS capital_expenditures,
       max(value) FILTER (WHERE metric_key='free_cash_flow') AS free_cash_flow,
       max(value) FILTER (WHERE metric_key='dividends_paid') AS dividends_paid,
       jsonb_object_agg(metric_key,status) AS metric_status
FROM finance.metric_value WHERE statement_type='cash_flow_statement'
GROUP BY company_id,cik,ticker,company_name,filing_id,accession_number,form,filing_date,reporting_period_end,
         processing_family,processing_set_id,selection_reason,validation_status,period_id,period_start,period_end,duration_days,currency;

COMMENT ON VIEW finance.income_statement IS 'Income metrics as filed, one exact duration/currency per selected filing version. EPS is per share; other values are currency base units. NULL means inspect metric_status.';
COMMENT ON VIEW finance.balance_sheet IS 'Balance metrics as filed, one instant/currency per selected filing version. Debt excludes leases and requires all three reported borrowing components.';
COMMENT ON VIEW finance.cash_flow_statement IS 'Cash flows as filed, including year-to-date durations. Free cash flow is OCF less cash PP&E payments. Period boundaries and currency must match.';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sec_reader') THEN
   GRANT USAGE ON SCHEMA finance TO sec_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA finance TO sec_reader;
 END IF;
END $$;
