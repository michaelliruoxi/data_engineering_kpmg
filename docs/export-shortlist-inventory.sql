-- Jace acquisition inventory. Reviewed against main 821e268, not run on shared DB.
-- Run on an existing reader connection in DBeaver. No changes are made.
-- Broad inspection window: 2021-01-01 through 2026-10-28 inclusive.
-- The final five-year acceptance window still needs Emma's confirmation.
-- Save the single JSON result as shared_inventory.json (plain JSON, not a screenshot).
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '60s';
SET LOCAL lock_timeout = '3s';

WITH targets(ticker, cik) AS (
  VALUES ('AAPL','0000320193'), ('CASY','0000726958'), ('MCK','0000927653'),
         ('MDT','0001613103'), ('MSFT','0000789019'), ('MU','0000723125'),
         ('NKE','0000320187'), ('NTAP','0001002047'), ('PH','0000076334'),
         ('TTWO','0000946581')
), company_rows AS (
  SELECT t.ticker AS requested_ticker, t.cik AS requested_cik,
         c.id AS company_id, c.name, c.ticker AS stored_ticker,
         (c.id IS NOT NULL) AS company_found
  FROM targets t LEFT JOIN sec.companies c ON c.cik=t.cik
), filing_rows AS (
  SELECT t.ticker AS requested_ticker, c.cik, f.id AS filing_id,
         f.accession_number, f.form, f.filing_date, f.reporting_period_end,
         f.source_url,
         COALESCE((SELECT jsonb_agg(jsonb_build_object(
             'id',s.id,'role',s.role,'source_url',s.source_url,
             'local_path',s.local_path,'sha256',s.sha256,
             'size_bytes',s.size_bytes,'retrieved_at',s.retrieved_at
         ) ORDER BY s.role,s.source_url)
         FROM sec.source_documents s WHERE s.filing_id=f.id),'[]'::jsonb) AS source_documents,
         COALESCE((SELECT jsonb_agg(jsonb_build_object(
             'processing_set_id',p.id,'set_key',p.set_key,
             'validation_status',p.validation_status,'coverage',p.coverage,
             'counts',p.counts,'report_id',p.report_id,'chunking_version',p.chunking_version,
             'is_existing_preferred',EXISTS(SELECT 1 FROM sec.preferred_processing_sets pref
                 WHERE pref.filing_id=f.id AND pref.processing_set_id=p.id)
         ) ORDER BY p.set_key)
         FROM sec.filing_processing_sets p WHERE p.filing_id=f.id),'[]'::jsonb) AS legacy_processing_sets
  FROM targets t JOIN sec.companies c ON c.cik=t.cik
  JOIN sec.filings f ON f.company_id=c.id
  WHERE f.form IN ('10-K','10-Q')
    AND f.filing_date BETWEEN DATE '2021-01-01' AND DATE '2026-10-28'
)
SELECT jsonb_pretty(jsonb_build_object(
  'exported_at',now(),
  'read_only',current_setting('transaction_read_only'),
  'export_window',jsonb_build_object('basis','filing_date','since','2021-01-01','until','2026-10-28'),
  'scope_status','broad_inventory_only_not_final_acceptance_window',
  'companies',(SELECT jsonb_agg(to_jsonb(c) ORDER BY requested_ticker) FROM company_rows c),
  'filings',COALESCE((SELECT jsonb_agg(to_jsonb(f) ORDER BY requested_ticker,filing_date,accession_number)
                     FROM filing_rows f),'[]'::jsonb),
  'limits',jsonb_build_array(
      'Database records do not prove local file bytes are currently available.',
      'No Exhibit 21 source record does not prove SEC unavailability.',
      'Only legacy processing sets are included; Michael owns V4 document/source set acceptance.',
      'No processing set was chosen or changed by this query.'
  )
)) AS inventory_json;

ROLLBACK;
