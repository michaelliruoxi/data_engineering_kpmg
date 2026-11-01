-- Financial fact coverage for the ten-company shortlist, read from the database.
--
-- Read-only: runs inside a READ ONLY transaction and ends with ROLLBACK, so it works with
-- any sec_reader login (for example sec_bryce in DBeaver). One row per shortlist filing
-- in the database and fact version stored for it; a filing with no stored facts still
-- appears, with zero counts. Scope matches config/shortlist.json: original 10-K/10-Q,
-- filing dates 2021-01-01 through 2026-12-31.
--
-- Compare with the offline `python -m sec_pipeline.fact_coverage` report: a database row
-- shows what was loaded, not whether every SEC filing was loaded. Filings missing from
-- the database do not appear here at all; use the acquisition inventory for those.

BEGIN TRANSACTION READ ONLY;

WITH shortlist(ticker, cik) AS (
    VALUES ('AAPL', '0000320193'), ('CASY', '0000726958'), ('MCK', '0000927653'),
           ('MDT', '0001613103'), ('MSFT', '0000789019'), ('MU', '0000723125'),
           ('NKE', '0000320187'), ('NTAP', '0001002047'), ('PH', '0000076334'),
           ('TTWO', '0000946581')
), scoped AS (
    SELECT s.ticker, f.id AS filing_id, f.accession_number, f.form, f.filing_date,
           f.reporting_period_end
    FROM shortlist AS s
    JOIN sec.companies AS c ON c.cik = s.cik
    JOIN sec.filings AS f ON f.company_id = c.id
    WHERE f.form IN ('10-K', '10-Q')
      AND f.filing_date BETWEEN DATE '2021-01-01' AND DATE '2026-12-31'
), fact_stats AS (
    SELECT ff.filing_id, ff.source_document_id, ff.extraction_version,
           count(*) AS facts,
           count(*) FILTER (WHERE ff.period_kind = 'duration') AS duration_facts,
           count(*) FILTER (WHERE ff.period_kind = 'instant') AS instant_facts,
           count(*) FILTER (WHERE ff.period_kind = 'forever') AS forever_facts,
           count(*) FILTER (WHERE ff.numeric_value IS NOT NULL) AS numeric_facts,
           count(*) FILTER (WHERE ff.is_nil) AS nil_facts,
           count(*) FILTER (WHERE ff.unit IS NULL AND NOT ff.is_nil) AS text_facts,
           count(DISTINCT ff.unit) AS distinct_units,
           count(*) FILTER (WHERE jsonb_array_length(ff.dimensions) > 0) AS dimensional_facts,
           count(*) FILTER (WHERE ff.label IS NOT NULL) AS labelled_facts,
           min(btrim(ff.raw_value)) FILTER (WHERE ff.concept_name = 'DocumentType' AND d.is_dei) AS dei_document_type,
           min(btrim(ff.raw_value)) FILTER (WHERE ff.concept_name = 'DocumentPeriodEndDate' AND d.is_dei) AS dei_period_end,
           min(btrim(ff.raw_value)) FILTER (WHERE ff.concept_name = 'DocumentFiscalYearFocus' AND d.is_dei) AS fiscal_year,
           min(btrim(ff.raw_value)) FILTER (WHERE ff.concept_name = 'DocumentFiscalPeriodFocus' AND d.is_dei) AS fiscal_period
    FROM sec.financial_facts AS ff
    JOIN scoped AS s ON s.filing_id = ff.filing_id
    CROSS JOIN LATERAL (SELECT ff.concept_namespace LIKE 'http://xbrl.sec.gov/dei/%'
                               AND ff.dimensions = '[]'::jsonb AND NOT ff.is_nil AS is_dei) AS d
    GROUP BY ff.filing_id, ff.source_document_id, ff.extraction_version
), anchors(anchor, concept) AS (
    VALUES ('revenue', 'Revenues'), ('revenue', 'RevenueFromContractWithCustomerExcludingAssessedTax'),
           ('revenue', 'RevenueFromContractWithCustomerIncludingAssessedTax'), ('revenue', 'SalesRevenueNet'),
           ('net_income', 'NetIncomeLoss'), ('net_income', 'ProfitLoss'),
           ('eps_diluted', 'EarningsPerShareDiluted'),
           ('operating_cash_flow', 'NetCashProvidedByUsedInOperatingActivities'),
           ('operating_cash_flow', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'),
           ('assets', 'Assets'), ('liabilities_and_equity', 'LiabilitiesAndStockholdersEquity'),
           ('equity', 'StockholdersEquity'),
           ('equity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'),
           ('cash', 'CashAndCashEquivalentsAtCarryingValue'),
           ('cash', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents')
), anchor_hits AS (
    -- Undimensioned, non-nil us-gaap anchors whose period ends on dei:DocumentPeriodEndDate.
    SELECT fs.filing_id, fs.source_document_id, fs.extraction_version,
           array_agg(DISTINCT a.anchor ORDER BY a.anchor) AS found
    FROM fact_stats AS fs
    JOIN sec.financial_facts AS ff
      ON (ff.filing_id, ff.source_document_id, ff.extraction_version)
       = (fs.filing_id, fs.source_document_id, fs.extraction_version)
    JOIN anchors AS a ON a.concept = ff.concept_name
    WHERE ff.concept_namespace LIKE 'http://fasb.org/us-gaap/%'
      AND ff.dimensions = '[]'::jsonb AND NOT ff.is_nil
      AND fs.dei_period_end ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
      AND COALESCE(ff.period_end, ff.instant_date) = fs.dei_period_end::date
    GROUP BY fs.filing_id, fs.source_document_id, fs.extraction_version
), receipts AS (
    SELECT filing_id, string_agg(set_key || ' ' || validation_status || ' facts=' || (coverage->>'facts'),
                                 '; ' ORDER BY set_key) AS processing_sets
    FROM (SELECT filing_id, set_key, validation_status, coverage FROM sec.filing_processing_sets
          UNION ALL
          SELECT filing_id, set_key, validation_status, coverage FROM sec.source_processing_sets) AS sets
    WHERE filing_id IN (SELECT filing_id FROM scoped)
    GROUP BY filing_id
)
SELECT s.ticker, s.form, s.filing_date, s.accession_number, s.reporting_period_end,
       fs.extraction_version,
       COALESCE(fs.facts, 0) AS facts,
       fs.duration_facts, fs.instant_facts, fs.forever_facts,
       fs.numeric_facts, fs.nil_facts, fs.text_facts, fs.distinct_units, fs.dimensional_facts,
       fs.labelled_facts,
       fs.dei_document_type, fs.dei_period_end, fs.fiscal_year, fs.fiscal_period,
       (fs.dei_document_type = s.form) AS dei_form_agrees,
       (fs.dei_period_end = s.reporting_period_end::text) AS dei_period_agrees,
       COALESCE(cardinality(h.found), 0) AS anchors_found_of_8,
       array_to_string(h.found, ' ') AS anchors_found,
       r.processing_sets
FROM scoped AS s
LEFT JOIN fact_stats AS fs ON fs.filing_id = s.filing_id
LEFT JOIN anchor_hits AS h
       ON (h.filing_id, h.source_document_id, h.extraction_version)
        = (fs.filing_id, fs.source_document_id, fs.extraction_version)
LEFT JOIN receipts AS r ON r.filing_id = s.filing_id
ORDER BY s.ticker, s.filing_date, s.accession_number, fs.extraction_version;

ROLLBACK;
