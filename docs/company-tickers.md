# Company ticker metadata

`sec.companies.ticker` is a current display symbol matched to the company's own
CIK using the SEC's `company_tickers_exchange.json`. It is separate from the
historical issuer name and identity stored with each filing.

Refresh the local reference and fill existing blank ticker fields:

```powershell
.venv\Scripts\python.exe scripts/sync_company_tickers.py --refresh --universe data/backfill/2026-10-13/universe.json --env-file .env.aws --database sec_filings --apply
```

Set `EDGAR_IDENTITY` in the environment before `--refresh`. Omit `--apply` to
preview database changes. Omit `--refresh` to reuse the downloaded reference.
The command requires an explicit environment file and database for database
access; it fills only NULL tickers and preserves existing symbols, company names,
CIKs, IDs, filings, facts, and citations.

The reference at `data/reference/company_tickers.json` records all symbols and
exchanges returned by the SEC, the retrieval time, source URL, and source hash.
The original SEC JSON is archived beside it. Set `SEC_TICKER_REFERENCE` to use
a different reference path for imports. Refreshing a different path requires
setting that same environment variable when starting the importer.

For S&P 500 members, the display symbol uses the first share class in the index
snapshot that also appears in the SEC data. Dot/hyphen aliases are matched, and
the stored spelling is the SEC spelling. Other issuers use the SEC file's first
listed symbol. This may be preferred stock or another listed security; it does
not assert that every issuer has publicly traded common stock. All alternative
symbols remain in the reference rather than being combined into one ticker.

New company registrations use this local reference automatically. They do not
make network or model calls. Existing running Python processes retain the code
they loaded at startup; their newly inserted blank tickers can be filled with
another synchronization, and their next run uses the updated importer.

An issuer absent from the current SEC ticker file stays NULL. This often applies
to subsidiaries, operating partnerships, or predecessor entities. Never fill
that gap with a parent's or successor's ticker: those securities belong to a
different CIK. Historical symbols require separate evidence and effective dates.

The SEC updates the mapping periodically and does not guarantee its scope or
accuracy: [SEC ticker and exchange associations](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).
