# Developer usage

This guide covers Python access, SQL queries, local development, and the SEC exporter. For a walkthrough that does not require code, use the [main README](../readme.md). You still need an assigned database login and an approved source IP before connecting to RDS.

## First SQL query

Connect to the shared database using the [main README](../readme.md), then open a SQL editor for that connection. Run each statement below through its semicolon. These statements also work against a migrated and seeded local database.

```sql
SELECT current_database() AS database_name, current_user AS connected_user;

SELECT company_name, form, filing_date, accession_number, source_document_count
FROM sec.filing_catalog
ORDER BY filing_date DESC, accession_number
LIMIT 20;
```

The catalog should include the Microsoft 10-Q with accession `0001193125-26-191507` and `source_document_count = 4`. The shared database also contains the [manual formatted example and six read queries](manual-example.md), version `manual-example-v1`, for facts, statement excerpts, report text, and citations. Running the local metadata seed does not install that manual example.

## Python access

Get a copy of this repository, install [uv](https://docs.astral.sh/uv/getting-started/installation/), and open a terminal in the repository's root folder. Install the locked dependencies:

```powershell
uv sync --locked
```

Python 3.12.14 and edgartools 5.59.1 are pinned for reproducibility.

Save the CA bundle at `.cache/aws-rds/us-east-1-bundle.pem` inside the repository. On Windows, these PowerShell commands create the folder and download the certificate:

```powershell
New-Item -ItemType Directory -Force .cache\aws-rds | Out-Null
Invoke-WebRequest -Uri 'https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem' -OutFile .cache\aws-rds\us-east-1-bundle.pem
```

Create a file named **`.env.aws`** in the repository root with the following content. Replace the username and password placeholders locally. If this file already exists, keep its working settings instead of overwriting it.

```dotenv
PGHOST=sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com
PGPORT=5432
PGDATABASE=sec_filings
PGUSER=YOUR_DATABASE_USERNAME
PGPASSWORD=CHANGE_ME
PGSSLMODE=verify-full
PGSSLROOTCERT=.cache/aws-rds/us-east-1-bundle.pem
```

`.env.aws` is ignored by Git. Keep passwords out of source code, commits, screenshots, and shared connection URLs. Values in this file are literal; do not use shell variables or inline comments.

Start Python with `uv run --locked python`, then paste this example. Enter a blank line after the last line to run the block:

```python
from sec_pipeline.database import connect

with connect(".env.aws") as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    print(conn.execute("SELECT current_database(), current_user").fetchone())
    rows = conn.execute("""
        SELECT company_name, form, filing_date, accession_number, source_document_count
        FROM sec.filing_catalog
        ORDER BY filing_date DESC, accession_number
        LIMIT 20
    """).fetchall()
    for row in rows:
        print(row)
```

The first printed row identifies the database and your login; the remaining rows are the same catalog results as the SQL example. `SET TRANSACTION READ ONLY` prevents this example from writing data.

Run from the repository root so both `.env.aws` and the certificate path resolve correctly. Calling `connect()` without the filename uses the local `.env` instead. Existing process `PG*` environment variables override matching file settings; `DATABASE_URL` is not used by this project's helper.

## Connection diagnostics

On Windows, test TCP reachability separately from authentication:

```powershell
Test-NetConnection sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com -Port 5432
```

`TcpTestSucceeded: True` confirms the network path only. Use the SQL example to verify database permissions. A permission error from `sec-db status` does not necessarily mean a view-only reader cannot connect.

For DBeaver versions that expose SSL under Driver properties, use `sslmode=verify-full` and `sslrootcert=<full path to the AWS PEM file>`. Keep hostname and certificate verification enabled.

## Local development and database administration

See [database setup and loader contracts](database.md) for local Docker setup, the schema, write interfaces, tests, and backup/restore. See [AWS deployment and maintenance](aws-deployment.md) for the hosted database.

The `sec-db` command provides `migrate`, `seed-manifest`, and `status`. Migrations and ingestion are maintainer or loader operations. `status` reads the migration ledger and every data table, so a login limited to views may not have permission to run it; use the catalog query above as the connection test. Database ingestion reads local files and does not contact SEC.

## Verify the sample

```powershell
uv run --locked python scripts/download_sec_sample.py --verify-only
uv run --locked python -m unittest discover -s tests -v
```

The included Microsoft sample, accession `0001193125-26-191507`, is stored under `data/raw/sec/`. Its source URLs, file sizes, and SHA-256 hashes are recorded in `source_manifest.json`. Verification runs locally without SEC requests.

## Export an income statement

This command makes SEC requests and exports an income statement from the selected ticker's latest available 10-K to a new CSV. The AAPL example below does not process the bundled Microsoft 10-Q or load any database records.

Approve the name and contact email to send to SEC, then supply them at runtime:

```powershell
$env:EDGAR_IDENTITY = Read-Host 'Approved SEC User-Agent: Full Name contact email'
try {
    uv run --locked sec-pipeline --ticker AAPL --output data/processed/AAPL-income.csv --identity-approved
    if ($LASTEXITCODE -ne 0) { throw 'Income statement export failed.' }
} finally {
    Remove-Item Env:EDGAR_IDENTITY -ErrorAction SilentlyContinue
}
```

Each row includes the selected filing's accession number, URL, form, and ticker. Existing output files are never overwritten. New exports under `data/processed/` are ignored by Git.

Use `uv run --locked sec-pipeline --help` for options. The sample downloader has a fixed scope and integrity checks; the exporter uses edgartools' network behavior. These checks do not audit financial values.
