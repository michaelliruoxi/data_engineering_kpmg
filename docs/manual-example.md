# Manual database example

This example lets teams try the database's financial facts, statement tables, report text, and citations before the processing pipeline is built. It uses selected real content from Microsoft's 10-Q, accession **`0001193125-26-191507`**, filed April 29, 2026, for the period ending March 31, 2026.

All example records use **`manual-example-v1`** as their extraction or chunking version. This is a partial, manually prepared example. It does not represent a complete filing, a complete financial statement, or validated pipeline output. No processing pipeline is implemented by this example.

## What to open

Connect to the shared AWS database using the [database access guide](../readme.md). Use your assigned login and an approved network connection. The local Docker database is separate; select the shared RDS connection in DBeaver.

In DBeaver, select the shared connection and open **SQL Editor → New SQL Editor**. Open [manual-example-queries.sql](manual-example-queries.sql), copy one numbered query into the editor, select the whole query through its semicolon, and choose **SQL Editor → Execute SQL Statement**. Read the rows in the result grid below the editor. Each query is read-only and works independently; no Python setup or earlier query result is required. See DBeaver's [SQL editor](https://dbeaver.com/docs/dbeaver/SQL-Editor/) and [query execution](https://dbeaver.com/docs/dbeaver/SQL-Execution/) guides if your menu layout differs.

| Query | What it shows | Expected result for this sample |
| --- | --- | --- |
| 1. Sample overview | Filing details and counts filtered to this example version | 1 row: 4 source documents, 1 report, 6 chunks, 24 facts, 3 tables, 24 links |
| 2. Financial facts | Values, periods, units, dimensions, and original source references | 24 rows |
| 3. Statement excerpts | Readable statement text, structured JSON, and the HTML source citation | 3 rows: income statement, balance sheet, and cash flow |
| 4. Table-to-fact links | Which fact records belong to each statement excerpt | 24 rows |
| 5. Report excerpt | The selected report text and its section descriptions | 1 row with `section_count = 2` |
| 6. Chunks and citations | Smaller passages, source links, character positions, and a check against the report text | 6 rows, each with `text_matches_report = true` |

These expected results were recorded when the sample was loaded and verified in the shared database on **October 6, 2026 (New York time)**. Query 1 counts processed records only for `manual-example-v1`; its `source_document_count` counts the filing's original files, which are shared across extraction versions.

If query 1 returns one filing with zero sample counts, that database has the filing metadata but not this sample. If it returns no rows, the filing itself is absent. Confirm that the editor is attached to shared RDS rather than the separate local database. For **Permission denied**, send the administrator the query number and error message; a login with access only to views may not be able to run the queries that also read tables.

The income and cash-flow excerpts compare January-March 2026 with January-March 2025. The balance-sheet excerpt compares March 31, 2026 with June 30, 2025. The report contains selected MD&A passages about quarterly results and liquidity and capital resources; its cash-flow discussion covers nine months, so it has a different period from the quarterly table excerpt.

The load verification checked all 24 facts and displayed cells against the original XML and HTML, including units, periods, scales, and signs. Both balance-sheet equations reconciled. All six passages matched their source paragraphs and stored text ranges. The insert committed as one transaction, and a fresh connection confirmed the saved records and unchanged preexisting metadata. All six queries succeeded under each teammate's reader role. These role checks used the administrator's existing connection; they did not test the teammates' passwords or connectivity from their own networks.

## Read the values correctly

`numeric_value` stores the numeric value in the fact's stated unit. For plain USD facts, that means dollars; the statement may display the same amount in millions. Query 2 includes a separate `value_usd_millions` column only when the stored unit is plain USD. For example, find `RevenueFromContractWithCustomerExcludingAssessedTax` with `period_start = 2026-01-01` and `period_end = 2026-03-31`: its `numeric_value` is **82,886,000,000 USD**, and `value_usd_millions` is **82,886**, matching total revenue in query 3's income-statement excerpt.

`raw_value` retains the source value as text; it is not necessarily the displayed statement cell. Parentheses in statement text indicate a negative amount: the 2026 quarterly financing cash flow displays `(11,351)` million and is stored as **-11,351,000,000 USD**. A `NULL` value is not zero; for example, query 2 leaves `value_usd_millions` empty for units other than plain USD. Reported rounding information remains available in `reported_decimals` and `reported_precision`.

Keep the period columns when using any amount. An income or cash-flow amount covers the dates in `period_start` and `period_end`; a balance-sheet amount is measured on `instant_date`. A quarter and a nine-month period can share an end date. Do not add overlapping periods or assume repeated concepts are duplicate records. Check the unit, dimensions, and source occurrence as well.

`financial_table_facts` records table membership. It does not itself identify the row or column containing a value. The table's `structure` JSON describes the selected cells and their source references. The JSON conventions in this example are illustrative and do not finalize the team's global extraction contract.

Report `full_text` contains only the selected excerpt. Chunk offsets refer to that stored text, using zero-based Unicode character positions with the end position excluded. They are not offsets into the original HTML. In Python, `full_text[offset_start:offset_end]` must equal the chunk's `text`; query 6 makes the equivalent check in PostgreSQL and should return `text_matches_report = true` for every chunk. This check proves that the chunk matches the stored report text, not that the report is complete or that extraction from the original source was correct.

## Trace the source

Start with the [SEC filing index](https://www.sec.gov/Archives/edgar/data/789019/000119312526191507/0001193125-26-191507-index.html). The original [HTML report](https://www.sec.gov/Archives/edgar/data/789019/000119312526191507/msft-20260331.htm) supplies readable statements and passages; the [XBRL instance](https://www.sec.gov/Archives/edgar/data/789019/000119312526191507/msft-20260331_htm.xml) supplies structured fact evidence.

The queries return `source_url`, `sha256`, and source-reference details so a value or passage can be checked against its original document. `local_path` identifies a file in the project checkout, not a downloadable database attachment. Connecting to RDS does not give you those file bytes. Use the SEC links or a matching project copy under `data/raw/sec/`; the hashes are recorded in [source_manifest.json](../source_manifest.json).

The manually prepared input files are retained locally under `data/examples/manual-example-v1/`, and before/after snapshots and verification evidence are under `backups/manual-example-v1-*.json`. These folders are ignored by Git; teammates can read the saved sample directly from RDS with the queries above. Later extracted output should use its own version, so readers can distinguish this example from the team's processing results.
