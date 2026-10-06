# SEC Filing Pipeline

Independent QMSS practicum workflow using public SEC filings and edgartools. Includes a verified Microsoft 10-Q sample and a command to export a company's latest 10-K income statement to CSV.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```powershell
uv sync --locked
```

Python 3.12.14 and edgartools 5.59.1 are pinned for reproducibility.

## Verify the sample

```powershell
uv run --locked python scripts/download_sec_sample.py --verify-only
uv run --locked python -m unittest discover -s tests -v
```

The included Microsoft sample, accession `0001193125-26-191507`, is stored under `data/raw/sec/`. Its source URLs, file sizes, and SHA-256 hashes are recorded in `source_manifest.json`. Verification runs locally without SEC requests.

## Export an income statement

Approve the name and contact email to send to SEC, then supply them at runtime:

```powershell
$env:EDGAR_IDENTITY = Read-Host 'Approved SEC User-Agent: Full Name contact email'
try {
    uv run --locked sec-pipeline --ticker AAPL --output data/processed/AAPL-income.csv --identity-approved
} finally {
    Remove-Item Env:EDGAR_IDENTITY -ErrorAction SilentlyContinue
}
```

Each row includes the selected filing's accession number, URL, form, and ticker. Existing output files are never overwritten. New exports under `data/processed/` are ignored by Git; `output.csv` is the original historical AAPL example.

Use `uv run --locked sec-pipeline --help` for options. The sample downloader has a fixed scope and integrity checks; the exporter uses edgartools' network behavior. These checks do not audit financial values.
