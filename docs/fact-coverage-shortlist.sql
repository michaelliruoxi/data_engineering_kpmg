-- Financial fact coverage for the ten-company shortlist, read from the database.
--
-- Run with a reader login on a database migrated through 010. The result has one row
-- per filing, XML source document and extraction version. A filing without stored
-- facts still appears with zero counts. The scope follows config/shortlist.json
-- for original 10-K/10-Q filings dated 2021-01-01 through 2026-12-31.
--
-- Compare the results with the acquisition inventory to identify filings missing
-- from the database. Each receipt applies to that row's source and version.

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
           count(*) FILTER (WHERE ff.label IS NOT NULL) AS labelled_facts
    FROM sec.financial_facts AS ff
    JOIN scoped AS s ON s.filing_id = ff.filing_id
    GROUP BY ff.filing_id, ff.source_document_id, ff.extraction_version
), first_dei AS (
    -- The extractor takes the first undimensioned, non-nil occurrence in XML order.
    SELECT DISTINCT ON (ff.filing_id, ff.source_document_id, ff.extraction_version, ff.concept_name)
           ff.filing_id, ff.source_document_id, ff.extraction_version, ff.concept_name,
           btrim(ff.raw_value) AS value
    FROM sec.financial_facts AS ff
    JOIN scoped AS s ON s.filing_id = ff.filing_id
    WHERE ff.concept_namespace LIKE 'http://xbrl.sec.gov/dei/%'
      AND ff.dimensions = '[]'::jsonb AND NOT ff.is_nil
      AND ff.concept_name IN ('DocumentType', 'DocumentPeriodEndDate', 'DocumentFiscalYearFocus', 'DocumentFiscalPeriodFocus')
    ORDER BY ff.filing_id, ff.source_document_id, ff.extraction_version, ff.concept_name,
             ff.source_reference->'document_order', ff.occurrence_key
), dei AS (
    SELECT filing_id, source_document_id, extraction_version,
           max(value) FILTER (WHERE concept_name = 'DocumentType') AS dei_document_type,
           max(value) FILTER (WHERE concept_name = 'DocumentPeriodEndDate') AS dei_period_end,
           max(value) FILTER (WHERE concept_name = 'DocumentFiscalYearFocus') AS fiscal_year,
           max(value) FILTER (WHERE concept_name = 'DocumentFiscalPeriodFocus') AS fiscal_period
    FROM first_dei
    GROUP BY filing_id, source_document_id, extraction_version
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
    SELECT d.filing_id, d.source_document_id, d.extraction_version,
           array_agg(DISTINCT a.anchor ORDER BY a.anchor) AS found
    FROM dei AS d
    JOIN sec.financial_facts AS ff
      ON (ff.filing_id, ff.source_document_id, ff.extraction_version)
       = (d.filing_id, d.source_document_id, d.extraction_version)
    JOIN anchors AS a ON a.concept = ff.concept_name
    WHERE ff.concept_namespace LIKE 'http://fasb.org/us-gaap/%'
      AND ff.dimensions = '[]'::jsonb AND NOT ff.is_nil
      AND to_char(COALESCE(ff.period_end, ff.instant_date), 'YYYY-MM-DD') = d.dei_period_end
    GROUP BY d.filing_id, d.source_document_id, d.extraction_version
), receipts AS (
    SELECT filing_id, fact_source_document_id, fact_extraction_version,
           string_agg(processing_family || ' ' || set_key || ' ' || validation_status || ' facts=' || (coverage->>'facts'),
                      '; ' ORDER BY processing_family, set_key) AS processing_sets
    FROM finance.filing_version
    WHERE filing_id IN (SELECT filing_id FROM scoped)
    GROUP BY filing_id, fact_source_document_id, fact_extraction_version
)
SELECT s.ticker, s.form, s.filing_date, s.accession_number, s.reporting_period_end,
       fs.source_document_id, fs.extraction_version,
       COALESCE(fs.facts, 0) AS facts,
       COALESCE(fs.duration_facts, 0) AS duration_facts, COALESCE(fs.instant_facts, 0) AS instant_facts,
       COALESCE(fs.forever_facts, 0) AS forever_facts, COALESCE(fs.numeric_facts, 0) AS numeric_facts,
       COALESCE(fs.nil_facts, 0) AS nil_facts, COALESCE(fs.text_facts, 0) AS text_facts,
       COALESCE(fs.distinct_units, 0) AS distinct_units, COALESCE(fs.dimensional_facts, 0) AS dimensional_facts,
       COALESCE(fs.labelled_facts, 0) AS labelled_facts,
       d.dei_document_type, d.dei_period_end, d.fiscal_year, d.fiscal_period,
       (d.dei_document_type = s.form) AS dei_form_agrees,
       (d.dei_period_end = to_char(s.reporting_period_end, 'YYYY-MM-DD')) AS dei_period_agrees,
       COALESCE(cardinality(h.found), 0) AS anchors_found_of_8,
       array_to_string(h.found, ' ') AS anchors_found,
       r.processing_sets
FROM scoped AS s
LEFT JOIN fact_stats AS fs ON fs.filing_id = s.filing_id
LEFT JOIN dei AS d ON (d.filing_id, d.source_document_id, d.extraction_version)
                    = (fs.filing_id, fs.source_document_id, fs.extraction_version)
LEFT JOIN anchor_hits AS h
       ON (h.filing_id, h.source_document_id, h.extraction_version)
        = (fs.filing_id, fs.source_document_id, fs.extraction_version)
LEFT JOIN receipts AS r ON (r.filing_id, r.fact_source_document_id, r.fact_extraction_version)
                         = (fs.filing_id, fs.source_document_id, fs.extraction_version)
ORDER BY s.ticker, s.filing_date, s.accession_number, fs.extraction_version, fs.source_document_id;

ROLLBACK;
