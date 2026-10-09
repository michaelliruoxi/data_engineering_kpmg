# Shared SEC Filing Database

This project stores information about company reports filed with the U.S. Securities and Exchange Commission (SEC). The database is online, so approved teammates can view the same information from their own computers.

## Choose how to connect

**You can use DBeaver, connect directly from Python, or use both.** Have your database credentials ready, then follow the option that fits your work.

| What you want to do | Which option to use | What you need |
| --- | --- | --- |
| Browse tables, inspect a few records, run SQL manually, or export a CSV | [Option A: DBeaver](#option-a-connect-with-dbeaver) | DBeaver, your database login, and the AWS certificate |
| Pull data into a script or notebook for analysis or a repeatable workflow | [Option B: Direct Python connection](#option-b-connect-directly-from-python) | This repository, its Python environment, your database login, and the AWS certificate |
| Check the data visually, then automate the analysis | [Use both together](#use-dbeaver-and-python-together) | Complete the setup for each option once |

Both options connect from your computer to the **same PostgreSQL database on AWS**. DBeaver displays query results in a desktop app; Python loads them into variables and DataFrames. Python does not connect through DBeaver, and DBeaver does not need to be installed or running for Python to work. Saving a connection in DBeaver does not configure Python automatically.

You do not need an AWS account or a local PostgreSQL server to use the shared database. Both options use the same assigned login and `verify-full` SSL with the AWS certificate. A read-only account has the same permissions in either tool.

## Database credentials

Use the **database username and password already shared with you**. Keep them private. If you cannot find your credentials or your login fails, contact Michael.

## Option A: Connect with DBeaver

Choose this option to work with the database through menus, tables, and a SQL editor. You can complete this section without setting up Python or downloading this repository.

### A1. Install DBeaver and download the certificate

1. Download [DBeaver Community](https://dbeaver.io/download/), choose the version for your computer, and install it.
2. Download the [AWS security certificate](https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem). This small file helps DBeaver check that it is connecting to the correct server.
3. Save it somewhere you can find again, such as a folder called **Database Access** in Documents. Keep the filename **`us-east-1-bundle.pem`**. Do not delete or move it after connecting.

If the certificate link opens a page of text instead of downloading a file, save that page with the filename above. It should end in `.pem`, not `.html` or `.txt`.

### A2. Enter the connection details

Open DBeaver and choose **Database → New Database Connection**. Select **PostgreSQL**, then click **Next**. PostgreSQL is simply the type of database we use.

Check whether DBeaver is asking for a **Host** or a **URL**. They use different formats. In Host mode, fill in Host, Port, and Database separately. In URL mode, use the full URL below instead.

| Box in DBeaver | What to enter |
| --- | --- |
| **Host** (Host mode only) | `sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com` — enter this only in the **Host** box. |
| **Port** | `5432` |
| **Database** | `sec_filings` |
| **URL** (URL mode only) | `jdbc:postgresql://sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com:5432/sec_filings` |
| **Username** | The database username the administrator gave you |
| **Password** | The matching database password |

Putting only the Host address into the **URL** box causes **Invalid JDBC URL**. The full URL includes the required `jdbc:postgresql://` beginning, port, and database name. Enter these values in DBeaver, not your web browser. The username and password are for this database, not for an AWS account.

### A3. Set up SSL and test the connection

In the same connection window:

1. Open the **SSL** tab. If you cannot see it, click **+** and select **SSL**.
2. If there is a **Use SSL** checkbox, turn it on.
3. Set **SSL Mode** to **`verify-full`**.
4. In **CA Certificate**, choose the `us-east-1-bundle.pem` file you saved in A1.
5. Leave **Client Certificate** and **Client Private Key** empty.
6. Click **Test Connection**. If DBeaver asks to download a PostgreSQL driver, allow it; this is the component it needs to talk to the database.
7. When the test succeeds, click **Finish**.

SSL protects the connection. The `verify-full` setting also checks the server's identity. Keep that setting enabled. If the options on your screen differ, ask the administrator to help with the [SSL settings](https://dbeaver.com/docs/dbeaver/SSL-Configuration/).

**Success at this step:** the connection test succeeds, and your new database connection appears in DBeaver's left-hand panel.

### A4. Open the filing list

You can now browse the data by clicking through the folders:

1. Expand your saved connection in the left-hand panel. It may be named **postgres**, or another name you chose. Open **Databases → sec_filings** if the Databases folder is shown.
2. Open **Schemas → sec → Views**. Think of `sec` as the folder containing this project's information.
3. Double-click **filing_catalog**.
4. Select the **Data** tab to display its rows.

`filing_catalog` is a prepared summary of the reports in the database. You do not need to write a query to view it. See [DBeaver's Data tab help](https://dbeaver.com/docs/dbeaver/Data-Editor/) if needed.

These column names may be unfamiliar:

| Column | What it means |
| --- | --- |
| `company_name` | The company that submitted the report |
| `form` | The type of report; `10-Q` means a quarterly report |
| `filing_date` | The date the report was filed |
| `accession_number` | The report's unique reference number |
| `source_document_count` | How many original source files are recorded for that report |

**What you should see:** the initial sample is one Microsoft quarterly report, reference `0001193125-26-191507`, with **4** in `source_document_count`.

The example loaded on October 6, 2026 contains **24 financial facts, 3 statement excerpts, 1 report excerpt, and 6 text chunks**, labeled `manual-example-v1`. These are selected real values and passages from the Microsoft filing; the team's complete extraction pipeline is still being built. The [example guide](docs/manual-example.md) explains how to read the values, run six ready-to-use queries, and check the expected results.

This example is in the **shared AWS database**. A local database created with the developer setup starts with filing details and four source-file records only. In either database, source-file records contain links and file details; connecting does not download the original documents.

The next time you want to use the database, open DBeaver, connect to the saved connection, and open **filing_catalog → Data** again.

### A5. Explore the individual tables

The filing list is a summary. To see the information behind it:

1. In the left-hand panel, open **your connection → Databases → sec_filings → Schemas → sec → Tables**. If your connection does not show a Databases folder, go directly to **Schemas → sec → Tables**.
2. Double-click a table name, such as **companies**.
3. Click its **Data** tab to see the rows.

Clicking the small arrow beside a table expands details such as its columns. To view the actual records, double-click the table's name and choose **Data**.

| Table | What you will find there |
| --- | --- |
| **companies** | Company names and identification numbers |
| **filings** | The reports each company filed, including dates and reference numbers |
| **source_documents** | Details and source links for the original files belonging to each report |
| **reports** | Processed text extracted from the original documents |
| **chunks** | Smaller sections of that text, used to find and cite specific passages |
| **financial_facts** | Individual financial figures, such as revenue, with their dates, units, and sources |
| **financial_tables** | Extracted financial statement tables |
| **financial_table_facts** | Links showing which financial figures belong to which statement tables |
| **schema_migrations** | The database's technical update history; you can skip this when browsing the data |

One company can have many filings. Each filing can have several source documents. As the team processes those documents, it adds report text, smaller text sections, and financial figures to the related tables.

Columns ending in **`_id`** connect related records. For example, a filing's **company_id** matches the **id** of its company, and a source document's **filing_id** matches the **id** of its filing. These long identifiers let the database keep the relationships clear.

#### Try this with the Microsoft sample

1. Open **companies → Data** and find Microsoft.
2. Open **filings → Data** and find report reference `0001193125-26-191507` in the **accession_number** column.
3. Open **source_documents → Data** to see the four original-file records from the imported sample. The **source_url** column contains the original web addresses.
4. Under **sec → Views**, open **fact_provenance → Data** for financial figures and their sources, or **chunk_citations → Data** for text passages and citations.
5. Under **sec → Tables**, open **financial_tables → Data** and read **readable_text** for the statement excerpts. Return to **Views → filing_catalog → Data** for the combined overview.

These tables and views can contain multiple filings and extraction versions. The [six sample queries](docs/manual-example-queries.sql) select the Microsoft filing and `manual-example-v1` for you, so their expected counts remain useful as the team adds more data. Query 1 shows the counts; query 2 shows the 24 facts; query 6 shows the six passages and checks that they match the stored report excerpt.

When reading a financial figure, keep its **unit and period** beside it. For example, a value stored in dollars differs from a statement displayed in millions, and a three-month amount differs from a nine-month amount even when they end on the same date. The [example guide](docs/manual-example.md#read-the-values-correctly) explains both cases. If you see **Permission denied**, ask the administrator for access to the specific table or view.

### A6. Understand the tabs and refresh the data

When you open a table, DBeaver provides different ways to look at it:

| Tab or button | What it does |
| --- | --- |
| **Data** | Shows the actual records in rows and columns. Use this for everyday browsing. |
| **Properties** | Describes the table: its column names, data types, and other settings. |
| **Diagram** | Shows how the table relates to other tables. |
| **Refresh / F5** | Reloads the information, for example after another team has added records. |

If you opened **chunks** and only see a list of column names such as `id`, `filing_id`, and `text`, you are looking at **Properties**. Select **Data** to switch to the records themselves.

### A7. Run a SQL query and export results

1. Select your saved shared-database connection in the left-hand panel.
2. Choose **SQL Editor → New SQL Editor** from the top menu. Some versions label this **New SQL Script**. Check that the editor is using your shared connection and database `sec_filings`. See [DBeaver's SQL editor guide](https://dbeaver.com/docs/dbeaver/SQL-Editor/).
3. Paste the statements below. Highlight the first statement, including its semicolon, and choose **SQL Editor → Execute SQL Statement**. Repeat for the second statement. On Windows, **Ctrl+Enter** runs the selected statement. See [SQL execution](https://dbeaver.com/docs/dbeaver/SQL-Execution/).

```sql
SELECT current_database() AS database_name, current_user AS username;

SELECT company_name, form, filing_date, accession_number, source_document_count
FROM sec.filing_catalog
WHERE accession_number = '0001193125-26-191507';
```

**Expected result:** the first query shows `sec_filings` and your assigned username. The second shows one Microsoft 10-Q with four source documents. Both statements only read data. You can also paste and run individual queries from the [six sample queries](docs/manual-example-queries.sql).

To save query results as a CSV:

1. Right-click inside the result grid and choose **Export Data**.
2. Select **CSV**, then **Next**. Review the columns and extraction options. Choose **Query the database** to export the complete query result; **Use fetched rows** includes only rows already loaded in the grid. Leave **Selected rows only** off unless you want a subset.
3. Continue through the format options, choose a local folder and a new filename, then click **Finish** on the final page. See [DBeaver's export guide](https://dbeaver.com/docs/dbeaver/Data-export/).

A CSV is a snapshot saved on your computer. Editing it does not update the database, and new database records do not appear in an existing CSV. Rerun the query and export again when you need a fresh copy.

## Option B: Connect directly from Python

Choose this option to query the database inside your Python workflow. Have your [database credentials](#database-credentials) ready, then start at B1; you can skip Option A entirely. The project's `connect()` helper uses `psycopg` to connect directly to PostgreSQL and reads your connection settings from `.env.aws`.

The examples below read data for analysis. Saving processed results back to the shared database requires separate write access from Michael.

### B1. Set up Python

Get a local copy of this repository and install [uv](https://docs.astral.sh/uv/getting-started/installation/). Open a terminal in the repository root: the folder containing `readme.md` and `pyproject.toml`. Run:

```powershell
uv sync --locked
```

This prepares the project's `.venv` environment using the pinned Python 3.12.14 and locked packages, including `psycopg` for PostgreSQL and `pandas` for working with tables. [uv runs project commands in this environment](https://docs.astral.sh/uv/guides/projects/), so you do not need to activate it manually when using `uv run` below.

### B2. Save your connection settings

On Windows, run these commands in PowerShell to save the AWS certificate inside the repository. If the file is already there, keep it.

```powershell
New-Item -ItemType Directory -Force .cache\aws-rds | Out-Null
if (-not (Test-Path .cache\aws-rds\us-east-1-bundle.pem)) {
    Invoke-WebRequest -Uri 'https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem' -OutFile .cache\aws-rds\us-east-1-bundle.pem
}
```

Create **`.env.aws`** in the repository root with the content below. Replace the username and password placeholders with your own credentials. If you already have a working `.env.aws`, keep its settings.

```dotenv
PGHOST=sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com
PGPORT=5432
PGDATABASE=sec_filings
PGUSER=YOUR_DATABASE_USERNAME
PGPASSWORD=CHANGE_ME
PGSSLMODE=verify-full
PGSSLROOTCERT=.cache/aws-rds/us-east-1-bundle.pem
```

If you already downloaded the certificate for DBeaver, you can reuse it: set `PGSSLROOTCERT` to its full path, such as `C:/Users/YourName/Documents/Database Access/us-east-1-bundle.pem`. Keep `verify-full` enabled. Values in this file are literal; do not add shell variables or inline comments. `.env.aws` and `.cache/` are ignored by Git. Keep passwords out of Python files, notebooks, and screenshots.

Use the same host, port, database, and personal login as DBeaver if you use both tools. The DBeaver `jdbc:postgresql://...` URL is not a Python connection setting; use the separate `PG*` fields shown above.

### B3. Run your first query

Save the following as **`database_workflow.py`** in the repository root. It checks the connection, finds the Microsoft filing, and loads its example financial facts into a pandas DataFrame, a table you can work with in Python.

```python
import pandas as pd
from psycopg.rows import dict_row
from sec_pipeline.database import connect

accession = "0001193125-26-191507"
version = "manual-example-v1"

with connect(".env.aws", row_factory=dict_row) as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    print(conn.execute(
        "SELECT current_database() AS database_name, current_user AS username"
    ).fetchone())

    filing = conn.execute("""
        SELECT company_name, form, filing_date, accession_number,
               source_document_count
        FROM sec.filing_catalog
        WHERE accession_number = %s
    """, (accession,)).fetchone()
    print(filing)

    cursor = conn.execute("""
        SELECT fact_id, concept_namespace, concept_name, label, numeric_value,
               unit, period_kind, period_start, period_end, instant_date,
               dimensions, occurrence_key, accession_number, extraction_version,
               source_url, sha256
        FROM sec.fact_provenance
        WHERE accession_number = %s AND extraction_version = %s
        ORDER BY concept_name, period_start, period_end, instant_date, occurrence_key
    """, (accession, version))
    facts = pd.DataFrame.from_records(
        cursor.fetchall(), columns=[column.name for column in cursor.description]
    )

print(f"Loaded {len(facts)} financial facts")
print(facts[["concept_name", "numeric_value", "period_start", "period_end"]].head())
```

Run it from the repository root:

```powershell
uv run --locked python database_workflow.py
```

**Expected result for the documented sample:** `database_name` is `sec_filings`, `username` is your assigned login, the filing is Microsoft's 10-Q with four source documents, and the script loads 24 facts from `manual-example-v1`. The connection closes when the `with` block ends; `facts` remains available for analysis. `SET TRANSACTION READ ONLY` prevents this example from changing database records.

The [`dict_row` option](https://www.psycopg.org/psycopg3/docs/api/rows.html#psycopg.rows.dict_row) gives each result column a name. The `%s` markers pass values separately from SQL; keep using [query parameters](https://www.psycopg.org/psycopg3/docs/basic/params.html) when changing the filing or version. Do not build queries with f-strings or string concatenation.

### B4. Use the results in your workflow

Add analysis below the query in the same script. For example, inspect USD facts for the three months ending March 31, 2026:

```python
from datetime import date

def is_usd(unit):
    return (
        isinstance(unit, dict)
        and unit.get("numerator") == ["{http://www.xbrl.org/2003/iso4217}USD"]
        and unit.get("denominator") == []
    )


quarterly_usd = facts.loc[
    facts["unit"].apply(is_usd)
    & facts["period_kind"].eq("duration")
    & facts["period_start"].eq(date(2026, 1, 1))
    & facts["period_end"].eq(date(2026, 3, 31))
]
print(quarterly_usd[["concept_name", "numeric_value", "dimensions"]])
```

Financial values arrive as Python `Decimal` values to preserve precision. Keep the unit, dates, dimensions, and source columns with each value; different concepts or dimensions should not be added together automatically. Missing values are not zero. See [how to read the sample values](docs/manual-example.md#read-the-values-correctly).

For a local CSV, append this to the same script:

```python
from pathlib import Path

Path("data/processed").mkdir(parents=True, exist_ok=True)
facts.to_csv("data/processed/msft-facts.csv", index=False, mode="x")
```

The export folder is ignored by Git. `mode="x"` prevents overwriting an existing file; choose a new filename for another export. The CSV is a local copy and does not update PostgreSQL. It stores structured fields such as units and dimensions as text; use the DataFrame for calculations that need those fields.

In VS Code, select this repository's `.venv\Scripts\python.exe` interpreter on Windows. In a notebook, use a kernel backed by the same environment. Keep the working directory at the repository root so the settings and certificate paths resolve correctly.

For other tasks, query `sec.chunk_citations` for text and citations or `sec.financial_tables` for statement excerpts. The [six sample queries](docs/manual-example-queries.sql) show the relevant joins and version filters. For writing extracted results, use the [project's loader contracts and transactions](docs/database.md#python-transactions) with an approved write-capable account.

### B5. Troubleshoot Python connections

| What you see | What to do |
| --- | --- |
| **`uv` is not recognized** | Install uv using the link above, then open a new terminal. |
| **`ModuleNotFoundError`** | Run `uv sync --locked` in the repository root, then use `uv run --locked python database_workflow.py`. Check the interpreter or notebook kernel if running from an editor. |
| **Set a non-placeholder password** | Replace `CHANGE_ME` in `.env.aws` and check that the file is in your working directory. |
| Python connects to the wrong database | Use `connect(".env.aws")`; plain `connect()` uses the local `.env`. Existing process `PG*` environment variables override matching file settings. `DATABASE_URL` is not read by this helper. |
| **Certificate error**, **timeout**, or **authentication failed** | Check the certificate path, host, port, and assigned credentials. The connection troubleshooting below also applies to Python. |
| The script loads **0 facts** | Confirm you connected to shared RDS and used the exact accession and version above. A local database may contain only filing metadata. |
| **Permission denied** when writing | Personal reader accounts allow queries. Ask Michael for the appropriate loader access before writing shared results. |

## Use DBeaver and Python together

1. Use DBeaver to inspect the tables and test a small `SELECT` query. Note the filing, extraction version, units, and periods you selected.
2. Set up Python separately using Option B and the same database credentials. You can point both tools to the same certificate file.
3. Put the query inside `conn.execute(...)` in your script. Replace values that change, such as accession numbers, with `%s` placeholders and pass their values separately, as shown in B3. In DBeaver, use actual quoted values as shown in A7; `%s` placeholders belong to the Python driver.
4. Compare the results using the same filters. Once they match, run the Python script whenever you need updated data; DBeaver can be closed.

For occasional manual work, a DBeaver CSV export may be enough. For a repeatable workflow, query from Python so each run reads the database directly. In either case, local DataFrames and exported files keep their existing contents until you query or export again. Changing a DataFrame does not write changes back to PostgreSQL.

## Connection troubleshooting

| What you see | What to do |
| --- | --- |
| **Invalid JDBC URL** | In DBeaver, **Host** takes the server address alone; **URL** takes the full `jdbc:postgresql://...:5432/sec_filings` value from A2. Python uses the `PG*` settings from B2. |
| The connection keeps waiting or says **timed out** | Check the host and port from A2 or B2. Your Wi-Fi, VPN, or local firewall may block database connections. Send Michael the error and network context so he can check the connection and RDS status; individual IPv4 approval is not required. |
| **Password authentication failed** | Check that you used the database username and password provided to you. Ask the administrator to check your login if it still fails. |
| A **certificate** or **SSL** error | Check the certificate's saved location: DBeaver selects it in the SSL tab; Python uses `PGSSLROOTCERT`. Use the host address exactly as shown above and keep `verify-full` enabled. |
| **Permission denied** | You connected, but your login is not allowed to open that item. Send its name to the administrator. |
| You cannot find **sec** or **filing_catalog** | Check that you opened `sec_filings`, then **Schemas → sec → Views**. Ask the administrator if it is missing. |
| Some views contain no rows | Press **F5**, clear old filters, and confirm you selected the shared RDS database. The manual example includes 24 facts and 6 chunks; the separate local database may contain only filing metadata. |

When asking for help, include the error message and which step you reached. Keep your password out of messages and screenshots.

## For developers and administrators

These guides are optional if you only want to view the data:

- [GitHub tutorial: personal branches, commits, and pull requests to main](docs/github-tutorial.md)
- [Python access, SQL examples, and project commands](docs/developer-usage.md)
- [Manual data example: six queries and expected results](docs/manual-example.md)
- [Database structure and local setup](docs/database.md)
- [AWS deployment and maintenance](docs/aws-deployment.md)
- [Team implementation plan](docs/team-implementation-plan.md)

This is a QMSS practicum project using public SEC filings. Additional tools for verifying source files and exporting income statements are described in the developer guide.
