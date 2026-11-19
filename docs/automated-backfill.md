# Automated S&P 500 filing import

The backfill targets original **10-K and 10-Q filings submitted from October 13,
2021 through October 13, 2026**, inclusive, for a fixed snapshot of current S&P
500 issuers. Membership comes from the dated, archived Wikipedia constituents
table, with its source URL and SHA-256 recorded. This is a community-maintained
membership source, not a licensed index feed. Multiple share classes are merged
by SEC CIK; the initial snapshot contains 500 issuers and 503 tickers.

This scope uses today's members throughout the five-year window and includes
explicitly verified predecessor issuers. Original CIKs, accessions, dates, names,
and source URLs remain unchanged. It does not reconstruct historical index
membership or include 10-K/A and 10-Q/A amendments.

## Acceptance checkpoint — October 13, 2026

Discovery completed for all 500 current issuers and five verified predecessor
jobs: **9,833 filings**. The latest database checkpoint contains **104 filings**,
with **154,136 facts, 430 statement-table records, 104 full reports, and 19,393
chunks** under production versions. These counts exclude the preserved
`manual-example-v1` sample. Table records include retained versioned alternatives;
they are not a count of distinct statements. There are 141 processing sets across
the three import profiles, and eight literal source-marker observations.

Microsoft, Apple, segmented statements, five cash-reconciliation filings, Ulta,
and Dollar General passed committed replay checks. Deliberately failed late
validation rolled back without changing the scoped stored rows. Migration 007
passed isolated PostgreSQL checks and preserved all existing business rows when
applied to the receiving database; reader access to the new objects was verified.

This is a checkpoint, not a completed five-year backfill. At the checkpoint the
main queue had 89 imports, 154 failures, and 9,590 pending entries; additional
acceptance imports already existed in the receiving database. Queue and database
counts differ until those entries are verified under the selected profile.
Unsupported layouts remain queued for repair. The saved queue and database can
advance beyond these numbers. Preferred selections have not changed automatically.

## What the importer does

1. Discover submissions from each company's SEC submissions API, including
   referenced historical submission files that overlap the requested window.
2. Download the filing index, primary inline HTML, extracted XBRL instance, and
   extension schema. Verify accession, CIK, form, report date, index links,
   namespaces, local paths, and hashes before accepting the archive.
3. Extract XBRL fact occurrences, three primary statements, report text, detected
   sections, and exact text chunks using the production filing adapter.
4. Validate HTML/XML correspondence, numeric values, table-cell fact references,
   report source locators, section bounds, and chunk coverage.
5. Commit one filing's metadata and outputs in one PostgreSQL transaction.
   A failed filing leaves no partial import. Separate filings can succeed while
   unsupported filings remain in the checkpoint for investigation and retry.

SEC requests share one sequential client with at least one second between
requests. Only transient server errors receive bounded retries. HTTP 401, 403,
429, or an access-block page stops the network session and preserves the queue.
The supplied contact identity remains runtime configuration, never source
metadata. See [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
and [access guidance](https://www.sec.gov/about/developer-resources).

## Verified predecessor history

The reviewed mapping in `config/issuer_history_expanded.json` adds five original
issuers and 64 filings, bringing discovery to **9,833 filings**. The first four
relationships remain in the earlier `config/issuer_history.json` snapshot so an
existing queue can verify that its history was extended without rewriting it.

| Current issuer | Original CIK | Succession date | SEC evidence |
| --- | --- | --- | --- |
| ExxonMobil | 0000034088 | 2026-07-01 | [Successor registration](https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/d71068d8k12b.htm) |
| BlackRock | 0001364742 | 2024-10-01 | [Successor registration](https://www.sec.gov/Archives/edgar/data/2012383/000119312524229601/d856271d8k12b.htm) |
| Bunge | 0001144519 | 2023-11-01 | [Reorganization filing](https://www.sec.gov/Archives/edgar/data/1144519/000110465923113122/tm2329110d1_8k.htm) |
| Ferguson | 0001832433 | 2024-08-01 | [Successor registration](https://www.sec.gov/Archives/edgar/data/1832433/000119312524191090/d850112d8k.htm) |
| Paramount Skydance | 0000813828 | 2025-08-07 | [Successor registration](https://www.sec.gov/Archives/edgar/data/2041610/000119312525175046/d841914d8k12b.htm) |

**Paramount has an accounting-basis break.** Its predecessor periods include
Paramount Global only; the successor periods beginning August 7, 2025 reflect
the combined company and pushdown accounting. These periods are not comparable
on an unchanged accounting basis. The importer associates the original filings;
it does not combine or restate their financial values. See Note 1 of the
[2025 annual report](https://www.sec.gov/Archives/edgar/data/2041610/000204161026000011/psky-20251231.htm).

Smurfit Westrock's accounting predecessor is Smurfit Kappa, and TKO's is
Zuffa/TKO OpCo. Their verified predecessor CIKs contain no 10-K/10-Q history.
WestRock and WWE are acquisition counterparts and are not substituted as the
historical reporting entity. Spin-off parents are not automatically included.

Evidence HTML is archived with a hash and retrieval receipt. Migration 006 adds
immutable `sec.issuer_successions` links and `sec.issuer_filing_history` for
querying current-company history while retaining original filing ownership.
The view follows direct predecessor links; it does not traverse longer chains.
The five configured relationships are direct. A filing-date cutoff at each
succession limits predecessor discovery. Amendments remain outside this scope.

```sql
SELECT current_cik, original_cik, company_name_as_filed, accession_number,
       form, filing_date, relationship, evidence_url
FROM sec.issuer_filing_history
WHERE current_cik = '0002115436'
ORDER BY filing_date, accession_number;
```

## Run and resume

Use the repository's locked environment. These PowerShell examples select a
stable snapshot and explicit date range. Replace the identity with your own
approved name and real email; never put database credentials in commands.

```powershell
$env:EDGAR_IDENTITY = 'Your Name your.email@organization.org'

uv run --locked python scripts/backfill_sec.py --discover `
  --issuer-history config/issuer_history_expanded.json `
  --universe data/backfill/2026-10-13/universe.json `
  --state data/backfill/2026-10-13/queue.json `
  --since 2021-10-13 --until 2026-10-13

uv run --locked python scripts/backfill_sec.py --dry-run --limit 10 `
  --tables-version tables-v3 `
  --universe data/backfill/2026-10-13/universe.json `
  --state data/backfill/2026-10-13/queue.json `
  --since 2021-10-13 --until 2026-10-13

uv run --locked python scripts/backfill_sec.py --ingest `
  --tables-version tables-v3 `
  --max-database-gib 14 `
  --universe data/backfill/2026-10-13/universe.json `
  --state data/backfill/2026-10-13/queue.json `
  --since 2021-10-13 --until 2026-10-13 `
  --env-file .env.aws --database sec_filings
```

Run the same command to resume. Verified existing archives are reused. Ingestion
skips an existing import only after checking its sealed processing selection,
source hashes, versions, and validation in the actual receiving database.
`--replay` reparses and checks an identical import through the storage contracts.
`--offline` uses only archived sources. `--download` archives without extraction.
`--company-limit` bounds additional company discoveries; `--limit` bounds filing
attempts, including failures. `--status` prints checkpoint counts.
Use `--discover --refresh-discovery` to begin a fresh submissions check for the
same fixed window. This resets company discovery checkpoints without discarding
filings or processing results. Resume an interrupted refresh with `--discover`
without the refresh flag. New discoveries invalidate earlier completion proofs.
Discovery records, for every issuer, when its submissions were read
(`discovered_at_utc`), the in-window periodic forms it does not queue
(`related_forms`: amendments, 10-KT/10-QT transition reports and NT late-filing
notices), any 10-K or 10-Q row it cannot represent (`unsupported_filings`, for
example a text-only primary document) and the longest gap between consecutive
reporting periods. `--status` and every summary list the consequences under
`coverage_warnings`: `discovery_window_still_open` while `--until` has not
passed for an issuer (a later run skips issuers already discovered, so rerun
with `--refresh-discovery` after the window closes), `related_periodic_forms_not_queued`,
`unsupported_filings_not_queued` and `reporting_period_gap` (more than 120
days). The warnings change no status and no exit code; an unsupported row no
longer fails discovery of the issuer's other filings.
Completed dry runs are reused only after source hashes, versions, and validation
checks still match. Pending work is attempted before earlier failures, so a
bounded resumed run makes progress past an unsupported filing. Status remains
readable while another process owns the queue's writer lock.
After repairing a parser or source issue, `--retry-failed-only` retries just failed
or blocked filings. Do not retry a blocked SEC request until its cause is resolved.
After a successful dry-run repair pass, `--ingest --validated-only` loads just
the validated entries, repeating source and database checks before committing.
It leaves other queued work intact and does not claim the whole backfill is done.
`--max-database-gib 14` checkpoints before starting another filing once this
database reaches 14 GiB. This leaves a margin under the observed 20 GiB allocation;
it measures this database, not instance-wide free storage or WAL. An individual
filing can take the database beyond the threshold, so choose a margin appropriate
to the source sizes and monitor instance capacity. Existing sealed receipts can
still be verified without new artifact writes. The CLI exits 2 with
`last_run.stopped_reason = 'database_size_limit'` when the limit is reached.

After increasing RDS storage and verifying the live allocation, pass the chosen
database cutoff to either launcher with `-MaxDatabaseGiB`. Both preflight and
ingestion use that same value; the default remains 14 GiB. For example, **only
after confirming at least 50 GiB is allocated**, a cutoff of 40 GiB leaves
10 GiB for other storage and growth between checks:

```powershell
# Read-only preflight; this does not start an import or resize RDS.
.\scripts\run_backfill.ps1 -CheckOnly -MaxDatabaseGiB 40
# Resume using the same cutoff after the preflight succeeds.
.\scripts\run_backfill.ps1 -MaxDatabaseGiB 40
# Alternatively, start the background launcher instead of the foreground run.
# .\scripts\start_backfill.ps1 -MaxDatabaseGiB 40
```

The headroom in this example is not a guarantee against storage exhaustion.
Check RDS `FreeStorageSpace` as well as database size. Increasing the command's
cutoff does not allocate AWS storage, and enabling autoscaling does not remove
the importer's explicit cutoff. Do not start a second worker to change the
limit; stop the existing worker normally, then resume with the selected value.

The membership JSON and archived HTML must remain together. Changing the
membership snapshot, date window, or form list requires a new queue. A queue
remembers the history mapping's path and hash. An explicit discovery run may
switch to a new history file only if it preserves all earlier relationships;
keep the earlier file unchanged. Ordinary runs reject a changed mapping. A queue
lock prevents concurrent writers; after an interrupted process, confirm it has
stopped before removing its `.lock` file. Atomic checkpoint replacement uses a
short bounded retry for temporary Windows file locks.

For a long unattended run on the ingest machine, `scripts\start_backfill.ps1`
starts the `tables-v3` import in the background with timestamped logs under
`data\backfill\2026-10-13\logs`. It refuses to start while the process named in
the lock is still running, removes the lock only when that process has stopped,
and runs the read-only `scripts\check_backfill_ready.py` preflight first. Use
`-RetryFailedOnly` to revisit earlier failures before continuing the pending
queue. Keep the machine awake; closing the session that started a foreground
run stops it and leaves a stale lock.

`-Offline` passes `--offline`: the run makes no SEC request, needs no
`EDGAR_IDENTITY`, reads only the sources already archived under `data\raw\sec`
and skips every filing that is not archived yet. `-ValidatedOnly` passes
`--validated-only` and cannot be combined with `-RetryFailedOnly`. Together
they load what an offline dry run accepted, which is how filings that failed
under an earlier parser are revisited without downloading them again:

```powershell
# 1. Re-check the earlier failures against the archived sources. No database, no SEC.
.\.venv\Scripts\python.exe -u scripts\backfill_sec.py --dry-run --offline --retry-failed-only `
  --tables-version tables-v3 `
  --universe data\backfill\2026-10-13\universe.json `
  --state data\backfill\2026-10-13\queue.json `
  --since 2021-10-13 --until 2026-10-13 --data-root .
# 2. Load the entries that validated, in the background.
.\scripts\start_backfill.ps1 -Offline -ValidatedOnly
```

A full run re-checks every filing already stored before it reaches new work,
newest first, which costs a few seconds per stored filing. `-PendingOnly` passes
`--pending-only`: the run processes only filings not attempted yet (`pending`
or `downloaded`), re-checks no stored import and retries no earlier failure. Use
it to resume after a stop; it cannot be combined with `-RetryFailedOnly` or
`-ValidatedOnly`, and such a run never reports the whole backfill complete:

```powershell
.\scripts\start_backfill.ps1 -PendingOnly -MaxDatabaseGiB 40
```

### Several workers at once

`scripts\start_parallel_backfill.ps1` splits the filings not attempted yet
into shard queues under `data\backfill\2026-10-13\shards` and starts one
background `--pending-only` importer per shard, each with its own writer lock
and its own log pair (`backfill_<timestamp>_queue-NN.*`). Do not use it while
the shared instance is a `db.t4g.micro`; see the database limit below.

```powershell
.\scripts\start_parallel_backfill.ps1 -Workers 4 -MaxDatabaseGiB 40
```

- Every filing of an issuer goes to the same shard, so two workers never write
  one company's rows. Annual reports count three times a quarterly report when
  the work is balanced.
- Each worker spaces its own SEC requests by one second, so the combined rate
  is one request per second per worker. `-Workers` accepts 2 to 8; the SEC
  allows ten requests per second from one address.
- The database, not this machine or the SEC, limits the worker count. The
  shared instance is a `db.t4g.micro` with 1 GiB of memory. It can exceed its
  baseline disk speed only while a burst allowance lasts (CloudWatch
  `EBSByteBalance%` and `EBSIOBalance%` for the instance), and an idle
  database refills that allowance at about 7.5% per hour. On November 17, 2026
  every run with more than one worker ended badly:
  - Eight workers were no faster than four. After ten minutes the server ran
    critically low on memory, stopped responding and restarted; the database
    was unavailable to everyone for about six minutes.
  - Four workers ran for 2 hours 20 minutes at about 6.5 seconds per filing
    while `EBSIOBalance%` fell to 2%. Minutes later the instance stopped
    responding and RDS restarted it.
  - Two workers took about 16 seconds per filing and emptied
    `EBSByteBalance%` in 70 minutes. The instance stalled until the workers
    were stopped.

  Committed filings were intact each time, but the workers hung on their
  connections instead of exiting, so detect a stall by logs going quiet. One
  worker (`start_backfill.ps1 -PendingOnly`) loads a filing in about 11
  seconds and lowers the allowance by about 5% per hour. On this instance
  class, run one worker, read both balances before starting and while it
  runs, and stop it before either falls below 30%. Use this launcher only
  after the instance class has been raised, and then watch the logs and both
  balances for the first 15 minutes.

  That rate held only while the indexes fitted in memory. On November 18,
  with the database at 12 GiB, the same single worker used about 0.25% of the
  allowance for a typical quarterly report and 0.5% to 0.75% for a large
  annual report, which the refill sustains at roughly 15 to 30 filings an
  hour. `pg_statio_user_indexes` showed the cause. The `id` columns default
  to random UUIDs, so each new row landed on a random page of every index
  that starts with its identifier: five such indexes on facts, cells and
  chunks accounted for about three quarters of all index block reads, and
  17% to 26% of their lookups missed the buffer cache. The 548 MiB index on
  (source document, version, occurrence key), whose rows arrive in order,
  missed 0.9%. Bulk storage therefore supplies time-ordered identifiers
  (`database.time_ordered_uuid`, UUID version 7) for the facts, cells and
  chunks it inserts, so new rows share the newest index pages. The column
  defaults and the schema are unchanged, and existing rows keep their
  identifiers.

  Measured the same afternoon, 40 minutes apart on the same database, for new
  filings loaded by one worker:

  | Per 1,000 rows inserted | Random identifiers | Time-ordered identifiers |
  | --- | ---: | ---: |
  | Index blocks read | 1,822 | 39 |
  | Full-page images in the write-ahead log | 1,643 | 66 |
  | Write-ahead log volume | 9.2 MiB | 1.9 MiB |
  | Buffers the background writer had to clean | 1,471 | 45 |

  Disk traffic fell from about 500 MiB to about 55 MiB per new filing, and
  the allowance stayed at 43% to 44% while 13 large filings were loaded in
  seven minutes. Re-importing a filing whose facts were stored with random
  identifiers still looks those facts up (406 index blocks read per 1,000
  rows, down from 1,739). `ANALYZE` on a large table reads 30,000 blocks
  (234 MiB) whenever autovacuum triggers it, which shows as a short burst of
  reads unrelated to the load.

  Seven minutes was too short a window to judge the allowance. Over the next
  four hours one worker loaded new filings in three slices of 14, 28 and 18
  minutes (36, 67 and 48 filings); each slice took the allowance from 38% to
  between 27% and 29%, and the refill back to 38% took about 90 minutes each
  time. That is about 30 new filings an hour, a fifth of the pace while a
  slice runs. The database volume moved only about 2.5 MiB/s during those
  slices (`ReadThroughput` plus `WriteThroughput`), which does not account
  for the allowance spent, so most of the cost is disk traffic the volume
  metrics do not show. It has not been identified.
- Stop the single-queue importer first; the launcher refuses to start while the
  main queue is locked. While shard queues exist, `backfill_sec.py` refuses to
  import the main queue directly (`--status` still works).
- Run the launcher again to resume: existing shards are reused, `-Workers` is
  ignored, and a shard whose worker is still running is left alone.
- The main `queue.json` does not change while workers run. Progress is in the
  shard queues and logs, and `report_backfill_status.py` stays accurate because
  it reconciles against the database rather than the queue's own statuses.
- When every worker has stopped, copy the results back. The merge refuses to
  run while any shard is locked and moves the shard files to
  `shards\merged-<timestamp>`:

```powershell
.\.venv\Scripts\python.exe scripts\shard_backfill_queue.py merge --state data\backfill\2026-10-13\queue.json
```

Earlier failures are not in any shard. After merging, retry them with
`.\scripts\start_backfill.ps1 -RetryFailedOnly`.

The SEC client retries a request up to twice, with a short backoff, after a
transient HTTP 500/502/503/504 response or a transient transport failure
(timeout, dropped or refused connection). A failure that outlasts the retries
stops the run with `last_run.stopped_reason = 'infrastructure_error'`; the
checkpoint is preserved, and the filing it stopped on stays `failed` until a
run without `-PendingOnly` retries it.

Exit status is **0** only when the requested phase is complete, **2** for a
partial phase, **3** for blocked SEC access, and **1** for a setup/runtime error.
`--status` itself exits 0 after successfully reporting an incomplete queue.
The checkpoint's counts do not establish current database contents; live queries
and validated processing sets provide that evidence.

### When statement tables fail

With `--tables-version tables-v3`, a filing whose statement tables cannot be
extracted or validated no longer loses its other content. If the full
`filing-import-v3` import fails for any reason other than an infrastructure
error, the backfill imports the same sources again as `filing-import-v3-text`:
facts, report, sections, and chunks with every check that applies to them, and
`tables` coverage `absent`. Infrastructure errors (lost connection, network,
disk) still stop the run with the checkpoint preserved. The full import's
failure is stored in the set's `configuration->'tables_unavailable'` and in the
queue entry's `result.tables_unavailable`. Such entries are
`validated_review_required` or `ingested_review_required`.

On later runs, a sealed text-only set counts as that filing's receipt, so the
tables are not retried automatically. After repairing the table parser, run with
`--replay` to attempt the complete import again; a successful complete import is
stored as a separate `filing-import-v3` set. Failed entries now also record
`error_detail`: the failing stage, the names of failed checks, and the message
of the innermost pipeline error (database and HTTP error text is never stored,
only its SQLSTATE).

In an offline check of 40 failed filings with archived sources, 39 passed every
text-only check; the remaining filing failed a fact correspondence check.

### Layouts accepted on corroboration

Since November 18, 2026 `tables-v3` resolves two layouts that used to leave
statement cells unresolved and send the filing to the text-only fallback. Only
the `tables-v3` adapter turns them on (`corroborated_layouts`), so `tables-v2`
and `tables-v4` to `tables-v8` produce what they produced before.

**A column header that cannot be read alone.** Every tagged number in the
column must already carry one period, shared by at least two concepts, and the
header must print nothing that contradicts it: the year is the period's end
year, a printed date is its end date, a printed month its end month, and a
printed length matches: within ten days for months and within four days for
weeks. A fiscal period that starts on a fixed calendar day and ends on a
Saturday ("Twenty-Six Week Periods Ended March 29, 2025", tagged from October
1) is a few days off the whole weeks its header names; a 53-week year is never
"52 weeks". A date printed without a space after its comma ("February
1,2025") is read as that date. One of these must also hold:

| Evidence (`anchor`) | Example header | Requirement |
| --- | --- | --- |
| `printed_duration` | "Six Months / 2025", "Three-Month Period Ended March 28, 2025", "Second Quarter / 2025" | The header prints the period's length and it matches |
| `year_to_date_from_reported_balance` | "Year to Date / 2025" | The period starts the day after a balance date the filing reports |
| `comparative_column` | "2024" or "March 30, 2024" beside a fully captioned column | Another column of the table has an established period of the same length (within seven days) ending one year earlier or later |
| `printed_balance_date` | "Fiscal Year Ended / December 28, 2025" on a balance sheet | The tagged date is printed in full |
| `fiscal_year_end` | "At the End of Year / 2024", "Fiscal Year End / 2025" on a balance sheet, tagged January 3 of the next year | The header prints only that year; the tagged date falls in the first seven days of the following January; and the filing itself shows a fiscal period to close on that date: an undimensioned fact covers 52 or 53 weeks or a whole year ending on it, or a period beginning the day after it |
| `caption_duration`, `caption_balance_date` | "2025" under the line "For the quarters ended March 31, 2025 and 2024"; "March 31, 2025" under "Three months ended March 31, 2025 and 2024"; "2025" under "October 31, 2025 and 2024" on a balance sheet | A caption shared by the columns prints the period's end, as a full date or as a month and day beside that year, and for a duration its length; the column prints exactly one year, the period's end year; and the table has a column for every year the caption names |

A caption is a header cell that names two or more years, or a line printed
between the statement's title and the table (the lines listed under "Lines
around a title" below; any other text ends the search). A caption for a
balance column must print no length.

Since November 19, 2026 three printed forms of a year are also read. A period
that ends in the first seven days of January may be headed by the year before:
"Fiscal Year / 2022" for the 52 weeks ended January 1, 2023. A year printed
without a space before the next word is that year ("2024As Adjusted"). And a
footnote marker raised beside a year (`2026<sup>1</sup>`, which reads "20261")
is left out when the header is compared with the tags; the header cell itself
keeps the text the page prints.

The accepted period is the tagged one, and the proof is stored in the table's
`column_period_evidence` with method `corroborated_printed_header`. A header
naming two durations, a missing year, or any printed value that differs from
the tags leaves the column unresolved, as before.

**A cash balance in a cash-flow column.** The components of the opening and
closing cash balance ("Cash and cash equivalents", "Restricted cash", "Total")
are tagged with a date under the statement's "... ended" headers. Such a tag is
accepted when its concept is a cash balance (its name contains `Cash`), and its
date is the column's last day and the row names no opening balance, or the day
before the column's first day and the row names no closing balance. A balance
on any other date stays unresolved. So does any other balance concept: a
liability such as dividends payable on a supplemental row that reports a flow
is a company tagging error, which only `tables-v8` records.

**Components of a cash balance, proved by their total.** A cash flow statement
may close with the components of its cash balance, and a component can be
tagged with a concept whose name does not say cash: "Funds held on behalf of
clients" (`FundsHeldForClients`), or performance bonds held as restricted cash
(`GoodFaithAndMarginDepositsWithBrokerDealers`). Since November 19, 2026 such
a balance is accepted when the page's own arithmetic proves it
(`cash-component-sum-v1`):

- a row is tagged with a total cash concept
  (`CashAndCashEquivalentsAtCarryingValue`,
  `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` or its
  disposal-group form), without a dimension;
- the rows directly above it each hold exactly one balance for every date of
  that row, in the same unit and without a dimension; a row of other dates, a
  flow, a heading or a cell with two different tags ends the block;
- there are at least two such rows, and for every date they add up exactly to
  the total;
- the component's date is the last day of its column's period, or the date of
  a dated column in a reconciliation section.

The components a cell relied on are recorded with the total in the table's
`cash_component_sums`. A block that does not add up proves nothing, and a
liability on a supplemental row stays unresolved as before.

**Statement selection.** The same header reading applies when candidate
statement tables are examined, so a statement whose columns need corroboration
is no longer dropped with "Statement segment has an unresolved column period".
And when the established selection and the strict cash alias
(`standard-cash-alias-v1`) both decline a filing, selection is tried once more
counting `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations`
as a closing cash balance. That is the standard total a statement closes on
when the company reports discontinued operations or assets held for sale. The
strict alias accepts it only under a few exact row labels; here each such cell
must still be a cash balance dated at an endpoint of its column, or match a
labelled opening or closing rule, when the statement is extracted. This attempt
comes last, so a filing either established selection accepts keeps its exact
segments and proof.

**Statement titles.** A table that no established rule recognizes as a
statement is examined once more with a wider reading of its title
(`recover_headings(..., wide=True)`). The whole text must still be the title;
prose that mentions a statement never matches. The wider reading accepts:

- a title printed after the filing's own registrant name, taken from its DEI
  `EntityRegistrantName` facts, with or without "and subsidiaries" ("The Cigna
  Group Consolidated Statements of Income"), and that name alone as a header
  line between a title and its table;
- a title followed by its units ("Consolidated Balance Sheets in thousands,
  except par values");
- the prefixes "interim" and "and" ("Condensed and Consolidated", "Consolidated
  and Combined");
- these complete wordings: "Statements of Net Income", "Statements of (Loss)
  Earnings", "Statements of Earnings and Comprehensive Earnings", "Statements
  of Income and Total Comprehensive Income", "Results of Operations", and
  "Statement of Condition" for a balance sheet.

**Lines around a title.** When all of that still leaves a filing rejected,
selection is tried one last time with two more readings. In the first, a table
that still has no title is read with `recover_headings(..., wide=True,
header_lines=True)`, which passes the lines a filing prints between a title and
its table. The whole line must be made of these:

| Line | Example |
| --- | --- |
| Units | "(Millions of Dollars, Except Share Data)", "(Unaudited; in millions, except per share amounts)" |
| Units naming the currency first | "(In US$ and in thousands, except share and per share data)" |
| Periods and dates | "March 31, 2025 and December 31, 2024", "For the Three- and Nine-Months Ended September 30, 2025 and 2024", "For the Three Months Ended March 30, 2024 and April 1, 2023, respectively" |
| The registrant's name, in either spelling of its legal form | "W. R. Berkley Corporation and Subsidiaries" for the DEI name "W. R. BERKLEY CORP"; "Emerson Electric Co. & Subsidiaries" |
| Running page headers, rules and page numbers | "Table of Contents", "Item 1. Financial Statements", "36" |

A line with any other word still ends the search. The same reading accepts a
title in the table's own header row beside such text; a title whose words are
printed without a space ("CONSOLIDATEDBALANCE SHEETS"); one carrying a footnote
marker ("Statements of Cash Flows(1)"); a title printed over two lines where
the break falls inside it ("... Statements of Operations" / "and Comprehensive
Income (Loss)"); and the cash flow statement titled "Consolidated Cash Flows".

**A balance sheet printed as two tables.** The established rules join an
assets table and a liabilities-and-equity table only when the second has no
title and no dates of its own, or when the filing prints "statement continues
on the next page". The last attempt also accepts the pair
(`tied-balance-totals-v1`) when all of these hold:

- both tables carry the same title and the same dates, for one entity;
- nothing but footnotes, page furniture and the repeated title stands between
  them: no table with a number, no other statement's title, no note heading;
- the first holds the assets (and at most the liabilities), the second the
  liabilities and equity;
- for every date, the one undimensioned `Assets` fact of the first equals the
  one undimensioned `LiabilitiesAndStockholdersEquity` fact of the second, in
  one currency.

The tables are stored as `balance_sheet.segment_1` and
`balance_sheet.segment_2`, with the tied facts in `balance_continuation`.

**An income statement printed once per period length.** A quarterly report may
print the income statement as one table for the quarter and another for the
year to date, each complete and under the same title. The last attempt accepts
them as `income_statement.segment_1` and `income_statement.segment_2`
(`period-tables-v1`) when all of these hold:

- every complete table carries the same title, for one reporting entity;
- each table holds periods of one length, and no two tables the same length;
- the columns of every table end on the same dates;
- any other table read as an income statement carries another title, and none
  is a continuation.

A second complete table for the same periods, or one under another title, is
still ambiguous and rejected.

**Page furniture between the parts of a cash flow statement.** The established
adjacency rule lets only text stand between a cash flow table and its
continuation. The last attempt also passes a small table without numbers, such
as a page number, under the same conditions as between the two tables of a
balance sheet.

**A statement continued on an untitled next page.** Many filings break a
statement at the page end and print the rest as a new table with no title,
after only a page number. Where the established rules find one incomplete
titled table of a kind, the last attempt reads the next table that holds a
number as its continuation (`page_continuation`, `page-continuation-v1`) when
all of these hold:

- no reading finds a title for the second table, and neither table lies inside
  a tagged text block (a note);
- nothing but footnotes and page furniture stands between them, as above;
- the second table prints the first table's column periods again, or prints no
  column headers and has the same columns, in which case each column takes the
  period the first table gives it (`header_inheritance`);
- both tables report one entity, together they hold every concept group, and
  the first shares its title with no other table read as that statement;
- the statement's own arithmetic holds across the page break for every period.

| Statement | Arithmetic that must hold |
| --- | --- |
| Balance sheet | The one total assets fact of the first table equals the one total liabilities and equity fact of the second (`tied-balance-totals-v1`) |
| Cash flow | Opening cash plus the operating, investing and financing totals and the exchange-rate effect equals closing cash; a tagged net change must agree with those totals |
| Income statement | Income before income taxes less income tax expense, both in the first table, equals a net income line of the second |

The tables are stored as `<kind>.segment_1` and `<kind>.segment_2` with the
tied facts in `page_continuation`. A cash flow statement is read this way only
when it is the single table found under a cash flow title.

**Dimensional detail on a continued cash flow page.** The established rule
joins the pages of a cash flow statement only when every fact on every page
has the scope of the first page: one entity, one unit and the same dimensions.
A page marked "(continued)" often adds rows of detail that carry a dimension:
cash held by consolidated entities, by segment or by location. The last
attempt joins such a page when all of these hold:

- the page also holds facts of the statement's own scope;
- every other scope on it has the same entity and the same unit, at least one
  dimension, and no other legal entity (`LegalEntityAxis`);
- each concept group the page covers is evidenced by a fact of the statement's
  own scope.

The added scopes are recorded as `detail_scopes` in the statement group. Every
other condition of the established rule (same title, same periods, adjacency,
ordered continuation, complete combined evidence) is unchanged.

**A note table under a statement's title.** A note sometimes repeats a
statement's title above a table of detail ("Condensed Consolidated Balance
Sheets" over a table of deferred revenue). The established rules then find two
tables under one title and reject the statement. The last attempt leaves such a
table out when it lies inside a tagged text block, is not marked as a
continuation and holds none of the statement's totals; the one complete table
outside the notes is then the statement, stored as a single table as before. A
note table that holds a statement total, or the same table outside a note,
still makes the statement ambiguous.

**Captions read where the page displays them.** A cell styled `display:none`
takes no room on the page. A statement usually hides the same spacer cells in
every row, so the physical grid and the page agree. When the caption row hides
fewer of them than the rows that carry the numbers, the grid puts each caption
further left than the page does:

| | Column 1 | Column 2 | Column 3 | Column 4 |
| --- | --- | --- | --- | --- |
| As displayed | Three Months Ended March 31, 2024 | Three Months Ended March 31, 2025 | Twelve Months Ended March 31, 2024 | Twelve Months Ended March 31, 2025 |
| In the physical grid | Three Months Ended March 31, 2024 | Twelve Months Ended March 31, 2025 | Twelve Months Ended March 31, 2024 | 2025 |

The second column's numbers are tagged with the three months, so the grid's
reading contradicts every tag in it and the statement stays unresolved. Only
then, and only when the page itself lines the rows up (every numbered row hides
the same columns, no cell spans rows, and every header row has the displayed
width of the numbered rows), the header rows are read where they are
displayed. That reading is taken only when every tagged column of the table
then gets exactly the period its tags carry; a single column that disagrees
leaves the table as it was. The stored grid, cell identifiers and header cells
are unchanged. `column_period_evidence` records `displayed_header_alignment`
with the physical and the displayed headings of each column whose headings
differ, and the numbers and `presentation_columns` carry the displayed
headings. Measured on the 3,489 filings archived on November 18: 14 more parse
cleanly (Aflac, Amazon, Equifax, Lockheed Martin, Microchip, Paramount
Skydance and T-Mobile), all 14 pass the complete offline dry run, and each of
the 2,915 that parsed cleanly before is byte-identical.

**Row labels right of an empty first column.** A statement may leave its first
grid column empty and print every row label in the next one. A label such as
"Property, plant and equipment, net of accumulated depreciation of $5,909 and
$5,788" then carries the tagged amounts of both balance dates in a column that
has no period, and those cells used to stay unresolved ("Unresolved column
period"). Since November 19, 2026 a column counts as a label column when the
header gives it no period and every cell printed in it below the header is
the first printed cell of its row and reads as words. A column that prints a
bare number anywhere, or anything after another printed cell, is not one.
Tagged amounts in such a label are linked as they are in the first column
(`resolved_label`), and a label that prints a digit without a tag ("Other
commitments and contingencies (Note 12)") is a label. The last selection
attempt reads candidate tables the same way.

**References to notes.** A column that only points to the notes is not a
column of amounts. An untagged cell is a label when its column has no period
and holds no tagged number anywhere, and it prints the reference in words
("(Note 3)", "Notes 6 and 7") or, under a heading that is exactly "Note" or
"Notes", the note numbers alone ("13", "4, 5"). The heading "Note Reference
Number" was already read this way.

**A balance dated at the period's end in an income statement.** An income
statement may close with a balance, usually the shares outstanding, in a row
that says so: "Shares outstanding at March 31," or "Actual shares outstanding
at period end". Such a tag is accepted when the row label names the period's
end in those words ("at period end", "at year-end", "as of the end of the
period") or by the month and day of the column's last day, with the year if it
prints one, and the tag's date is that day. A balance of any other day, or in
a row that names no period end, stays unresolved.

**A consolidated statement followed by pages for parts of the group.** A
filing may print its consolidated cash flow statement and then, on pages
marked as its continuation, the same statement for each business group. The
established rules find two complete tables under one title and reject the
statement. The last attempt takes the first table alone as the statement when
all of these hold:

- it is complete, not marked as a continuation, and all its numbers have the
  registrant's own scope, without any dimension;
- every other table under that title is marked as a continuation, shows the
  same periods, and holds only numbers of that registrant and unit that carry
  a dimension, never another legal entity (`LegalEntityAxis`);
- nothing but footnotes and page furniture stands between the tables.

A continuation page that holds a number of the whole entity, another period
or another registrant still makes the statement ambiguous.

Measured on the 5,295 filings archived on November 19: these readings make
120 more parse cleanly (among them Aon, Bank of America, Broadridge, Centene,
Chubb, CME Group, Marsh & McLennan, News Corp, NXP, S&P Global, Textron and
Trimble), all 120 pass the complete offline dry run, and each of the 4,446
that parsed cleanly before is byte-identical.

**Utility revenue.** When every reading above has declined the filing,
selection is tried a final time counting
`RegulatedAndUnregulatedOperatingRevenue`, the total operating revenue a
regulated utility reports, as the revenue line of an income statement.

**Combined filings.** A filing made for a parent and its subsidiaries selects
the reporting parent's statements by the registrant name printed with each
title (`joint_statements`). When that established reading rejects the filing,
it is read once more (`header_lines=True`) with:

- the parent's name with or without "The", in another spacing, or with its
  legal form in the other spelling ("American Electric Power Company, Inc. and
  Subsidiary Companies" for the DEI name "AMERICAN ELECTRIC POWER CO INC."). A
  printed name that fits two registrants names neither;
- the header lines listed above between the name, the title and the table;
- the wider titles;
- an element that prints several header lines ("Table of Contents", the name,
  the title), read line by line when every piece of its text is the whole
  text of an element of its own.

A line naming another registrant still gives the table to that registrant, and
any other line still ends the search.

Measured offline on the 3,305 filings archived on this machine:

| Before November 18 | After | Filings |
| --- | --- | ---: |
| Parsed cleanly | Byte-identical tables | 1,887 |
| Statements selected, unresolved cells | Parse cleanly | 364 |
| Rejected at statement selection | Parse cleanly | 517 |
| Statements selected, unresolved cells | Still not accepted | 191 |
| Rejected at statement selection | Still not accepted | 346 |

Filings already stored as text-only are not retried automatically; replay them
as described above.

## Versioned statements and shared filings

The additive `tables-v4` profile supports a verified set of original inline
documents under one accession. It archives a separate
`source_document_set_manifest.json`, verifies the union of inline occurrences
against the extracted XML, and keeps each HTML member's report, sections,
chunks, and source locators separate. It requires one unambiguous member
containing all three primary statement groups; unsupported statements split
across separate HTML documents remain a failure. The original four-source
manifest remains unchanged.

V4 requires migration 008 and uses `sec.source_processing_sets` and its explicit
member tables. The legacy preferred catalogs are unchanged. The source catalogs
expose exact archived values and the separate source and semantic statuses:

```sql
SELECT accession_number, validation_status, source_validation_status, counts,
       coverage, semantic_checks
FROM sec.source_processing_overview
ORDER BY filing_date DESC;

SELECT accession_number, table_key, cell_key, display_text, column_period,
       quality_issue_codes, quality_issues, review_source_facts
FROM sec.source_statement_cells
WHERE resolution_status = 'review_required';
```

An exact HTML/XML match can still have a printed-period conflict. Two bounded
cash-flow rules preserve those occurrences with their original values and
periods: an ancillary instant fact under a duration header, and an opening
balance tagged with the duration's first day instead of the preceding day.
Each exception requires a separately verified complete cash-flow core. The
original displayed period and tagged period are both recorded. A numeric
HTML/XML mismatch remains a hard failure. Review cells have source bindings
and quality issues rather than accepted statement-cell links.

`source_fact_catalog`, `source_report_catalog`, and `source_chunk_catalog`
retain source and quality provenance. `analysis_ready_facts` and
`analysis_ready_statement_cells` exclude sets requiring review; their names
refer only to the declared checks, not universal accounting validation. Report
coverage remains `unknown`: the cleaner's output and chunk boundaries are
verified, but browser-visible completeness and heuristic section labels are
not certified.

Select `--tables-version tables-v4` for this profile. Queue statuses
`validated_review_required` and `ingested_review_required` preserve the
distinction from a fully passed semantic check. `--validated-only` includes
source-verified entries requiring review and repeats every import check.
`last_run.review_required_successes` reports these separately. A completed
import phase means all source imports were verified, not that every semantic
issue was resolved. V4 dry runs always reconstruct the source payloads.

A dry run never rewrites an import. For an entry whose status starts with
`ingested`, the status, receipt (`result`), destination, versions and manifest
stay as recorded, and the dry-run outcome is stored separately under `dry_run`
(`status`, `result` or the failure detail, `processing_versions`,
`checked_at_utc`). `--retry-failed-only --dry-run` also selects entries whose
`dry_run.status` is `failed`. A bounded document-set dry run reconstructs
pending filings before the ones this profile has already reconstructed, so
repeated `--limit` runs reach the whole queue; completion still needs one
unbounded pass.

`--tables-version tables-v3` includes the V2 segmented layout and adds explicit
representations for untagged financial marker literals and verified hidden nil
bindings. It selects `filing-import-v3`; the table JSON and relational-cell
contract use schema version 2. A dash without a supported numeric binding stays
literal text with `resolution_status = 'source_only'` and a separate
`cell_source_observations` proof. It creates no numeric fact and is never inferred
to be zero or nil. A unique `-sec-ix-hidden` display reference can link an existing,
explicitly nil XML occurrence, retaining its source location and nil status.

The proof covers numeric and SEC hidden bindings in the archived DOM. Scripts
are not executed; external script presence is recorded as a scope limitation.
Stylesheet ambiguity and unsupported bindings remain unresolved. Validation
reconstructs the full source layout and representations independently, including
cell classifications and periods. Separate counts expose financial cells,
linked financial cells, literal markers, hidden nil cells, and unresolved cells.
SQL checks independently enforce these counts when a V3 set is sealed.
The hidden display-binding mechanism is specified in section 11.14 of the
[SEC EDGAR XBRL Guide](https://www.sec.gov/files/edgar/filer-information/specifications/xbrl-guide-2026-06-29.pdf).

```sql
SELECT accession_number, cell_key, display_text, source_only_evidence
FROM sec.statement_source_observations;

SELECT f.accession_number, s.counts, s.configuration->>'source_value_scope' AS scope
FROM sec.filing_processing_sets s JOIN sec.filings f ON f.id = s.filing_id
WHERE s.set_key = 'filing-import-v3';
```

`--tables-version tables-v2` selects the segmented adapter and the separate
`filing-import-v2` processing set. It preserves each physical table, its original
XPath, row/column coordinates, and fact links. A cash-flow statement spanning two
tables therefore produces two records under one `statement_type`, with ordered
`statement_group` metadata. Reconciliation segments retain their own instant
periods. Group validation checks original headers, periods, units, entity scope,
adjacency, and combined statement coverage. It rejects missing or ambiguous
segments. Single-table statements retain their ordinary keys.

The v1 adapter and `filing-import-v1` sets remain available. Existing facts,
reports, chunks, and manual examples are reused when identical; changed table
representations receive new versioned records. No old processing set or preferred
selection is overwritten. Without an explicit flag, the CLI retains the v1
profile for compatibility.

Some combined filings use a subsidiary's archive CIK while the reporting parent
appears under a different DEI context. The importer verifies the exact accession,
primary document, explicit index filers, and XML/inline DEI identity before using
that canonical archive. A dimensional co-filer must have one genuine LegalEntityAxis
member tied to an independently proven, undimensioned reporting registrant. Form
and period inheritance requires that same proof and rejects contradictions.
Original fact entities and dimensions remain unchanged. One shared accession
stays one filing. V2/V3 processing-set configuration retains the deterministic
`index_registrants` list. Verified index filers are registered transactionally so
the reporting parent can appear in `issuer_filing_history`. This association does
not automatically include earlier predecessor filings.

**An archive owner the XBRL never identifies.** United Rentals, Inc. files with
United Rentals (North America), Inc. and the SEC stores the report under the
subsidiary, but the XBRL states only the parent's CIK. FirstEnergy Corp. files
with Jersey Central Power & Light Company from 2025, and the DEI gives that
subsidiary a name under its legal-entity member and no CIK. The owner of such
an archive has no DEI identity to verify, and every one of these filings was
refused at download ("XML and inline registrant CIKs disagree or requested CIK
is absent"). The owner is now accepted as the one index filer the XBRL leaves
out (`registrants.unidentified_owner_name`), and only when:

- the index lists it as a filer and links the primary document in its archive;
- every other index filer is a DEI registrant proven as in any combined filing,
  including the single undimensioned registrant whose form and period equal
  the filing's, and the XML and inline documents agree on all of them;
- no DEI CIK fact and no context entity in either document names the owner.

The manifest then records the owner under the name the SEC index gives it
("UNITED RENTALS NORTH AMERICA INC"), because the filing states no name tied to
the owner's CIK; a name the documents print without a CIK is not used. The
requested registrant is verified exactly as before and is never accepted this
way. Still refused: a subsidiary context that repeats the parent's CIK (Public
Service Enterprise Group from its 2024 annual report), a parent whose DEI omits
the period end date (PPL, first quarter of 2025), and an annual report whose
primary document carries no DEI CIK (Wells Fargo, 2025).

Measured on November 19: the 15 refused filings of the two companies verify,
and in an offline dry run 14 pass under `tables-v3` and one loads as text for
review; the result of re-verifying every archived filing whose archive owner
differs from the queued company is unchanged.

The checkpoint's `last_run` describes the selected profile and destination.
Completion requires the current invocation to verify every queue entry; old
successful statuses cannot establish completion after changing the database or
extractor version. A bounded run or failure-only retry can therefore exit 2
until a complete verification pass finishes.

```sql
SELECT f.accession_number, t.statement_type, t.table_key,
       t.structure->'statement_group' AS segment_group, t.readable_text
FROM sec.financial_tables t JOIN sec.filings f ON f.id = t.filing_id
WHERE t.extraction_version = 'tables-v2'
ORDER BY f.filing_date DESC, f.accession_number, t.statement_type,
         COALESCE((t.structure->'statement_group'->>'segment_index')::int, 1);

SELECT f.accession_number, r->>'cik' AS registrant_cik, r->>'name' AS index_name
FROM sec.filing_processing_sets s JOIN sec.filings f ON f.id = s.filing_id
CROSS JOIN LATERAL jsonb_array_elements(s.configuration->'index_registrants') r
WHERE s.set_key = 'filing-import-v2';
```

## Single-filing verification and import

The existing `scripts/process_filing.py` now verifies the identity in each
manifest, so it accepts both forms and different issuers. The complete adapter
is `sec_pipeline.filing_adapter`, with explicit versions `facts-v1`, `tables-v1`,
`report-v1`, and `paragraph-v1`.

```powershell
uv run --locked python scripts/process_filing.py --dry-run `
  --manifest source_manifest.json --data-root . `
  --adapter-module sec_pipeline.filing_adapter `
  --facts-version facts-v1 --tables-version tables-v1 `
  --reports-version report-v1 --chunks-version paragraph-v1
```

Use `--ingest --env-file .env.aws --database sec_filings` in place of `--dry-run`
to write the selected filing. The CLI's existing processing-set and preferred
selection flags remain available. Batch imports record a `filing-import-v1`
processing set, but do not overwrite a preferred selection. Existing manual
sample versions remain available.

## Coverage and limitations

The source archive requires all four supported source roles. Unsupported source
layouts are reported as failures, never silently replaced with Company Facts.
The raw parser retains custom tags, dimensional contexts, units, periods, and
repeated occurrences.

Statement selection requires corroborating headings and US GAAP concepts.
Supported fiscal-week periods require agreement between independent source fact
contexts. Ambiguous, split, or unsupported statement layouts stop that filing.
The structured tables cover income, balance sheet, and cash flow; other tables
remain in report text. Table coverage is therefore recorded as `partial`.

Report rendering extracts visible content supported by the cleaner. Section
labels are heuristics and remain unverified. Report coverage is recorded as
`unknown`, rather than claiming independently established browser-visible
completeness. Chunk coverage is `complete` relative to the finalized report;
fact coverage is `complete` relative to the verified XML occurrences.

Large insert groups use bounded database pipelines, retaining the same immutable
content comparisons, source links, and filing-level transaction boundaries.

No recurring scheduler is installed by these commands. For new filing windows,
use a new dated membership snapshot and queue. Database/source identities prevent
duplicate records across queues. Keep the host running for a long backfill, and
retain the archived source bytes for provenance and later replay.

The October 13 capacity check confirmed 20 GiB of gp3 storage on the existing
RDS instance with storage autoscaling disabled. The 104-filing database checkpoint
used about 0.38 GiB; this excludes other instance storage such as WAL. These are
observed values, not a guarantee that the complete backfill fits. Capacity must
be tracked as the load advances. No instance settings were changed by the check.

## Receiving-database reconciliation (read-only)

Queue statuses are scheduling evidence, not proof of database imports. Use:

```sh
# Configure authorized libpq PG* / PGSERVICE / passfile settings privately.
# For shared RDS retain PGSSLMODE=verify-full and the approved PGSSLROOTCERT.
uv run --locked python scripts/report_backfill_status.py \
  --queue /private/queue.json --output /private/backfill-status.json
```

The report uses a read-only repeatable-read transaction, sealed legacy/source
processing receipts, and `finance.filing`'s published browsing selection. It
records the transaction timestamp, selected family/UUID/versions, all candidate
receipts, and one status per distinct accession across the queue/database union.
It never changes preferences or chooses an arbitrary version. An explicit
`--select ACCESSION=legacy:UUID` (or `source:UUID`) overrides only this report and
must belong to the accession. This is not approval of the processing choice.

- `fully_imported`: passed receipt and positive counts for all four stored
  artifact classes, with no source-quality or unaccounted-cell issues. The parser's
  primary-statement `tables=partial` and section-label `reports=unknown` declarations
  are retained; this describes completed ingestion, not exhaustive semantic coverage
  or original-source acceptance.
- `text_only`: passed facts/report/chunk receipt with zero/absent tables.
- `review_required`: review flags, incomplete artifact coverage, inconsistent counts,
  or stored candidates without a unique browsing selection.
- `failed`: queue failure/access block and no stored receipt.
- `pending`: no stored receipt; even a queue `ingested` claim cannot prove import.

Queue and database counts are separate. Multiple versions never become multiple
filings. A DB receipt absent from the queue and an imported queue entry absent
from the DB are explicit discrepancies. Requires migrations through 010 for
`finance.filing`; missing schema/permissions fail closed as unverified.

Add `--summary PATH` to also write the gap between the queue and the database,
and print its totals:

```sh
uv run --locked python scripts/report_backfill_status.py \
  --queue /private/queue.json --output /private/backfill-status.json \
  --summary /private/coverage-summary.json
```

The summary counts each queued filing once. `stored` is every filing with a
receipt in the database (`fully_imported`, `text_only` or `review_required`);
`missing` is `pending` plus `failed`. It gives those counts for the whole queue,
for every filing year and form, and for every issuer, with the issuers that
miss the most filings first. `issuers` says how many have all, some or none of
their queued filings stored. Filings the database holds outside the queue are
counted separately as `database_filings_outside_queue` and never as coverage.
Stored means a receipt exists, not that the filing passed every check: read the
status columns beside it. An `--offline` summary reports every filing as
`unverified` and neither stored nor missing.

With no DB access, add `--offline`: database count is null and each queue filing
is `unverified`, not pending or imported. Exit 2 also marks a failed DB read;
exception text/connection strings are withheld. These files may contain private
filing inventory and should remain outside Git. Current live counts have not
been verified in the 2026-10-29 isolated cloud review.

## Capacity: database ceiling versus instance free storage

`start_backfill.ps1 -MaxDatabaseGiB 25` passes the same 25 GiB ceiling to both
`check_backfill_ready.py --max-database-gib 25` and ingestion. The existing
launcher/preflight default remains **14 GiB**. Both launchers and the default
preflight cover only the October 13 `tables-v3` queue and migrations 001–009.
For another profile or queue, pass `--tables-version`, `--state`, `--universe`,
`--since` and `--until` to the preflight; it then requires the migrations and
tables that profile writes. See the
[reviewed rollout sequence](reviewed-layout-upgrade.md#rollout-sequence). The guard stops at or above the
ceiling, before the next filing transaction; a single transaction can still
cross it. Choose headroom for the largest expected filing plus index/temp growth.

`pg_database_size(current_database())` does **not** measure instance free space.
Before launch, use the already authorized AWS console or read-only CloudWatch
identity to check the **correct DB instance**'s `AWS/RDS` `FreeStorageSpace`
(Bytes, Minimum) over the last 15 minutes, with a 60-second period. For example:

```sh
# Substitute the verified region/DB identifier and current UTC ISO timestamps.
# This is a read-only measurement; do not use root or change infrastructure.
aws cloudwatch get-metric-statistics --region REGION \
  --namespace AWS/RDS --metric-name FreeStorageSpace \
  --dimensions Name=DBInstanceIdentifier,Value=VERIFIED_INSTANCE \
  --start-time UTC_15_MINUTES_AGO --end-time UTC_NOW \
  --period 60 --statistics Minimum --unit Bytes
```

Stop if the latest datapoint is absent/stale (>5 minutes), or below the
operator-approved reserve. Include other databases, WAL/logs, indexes, temp
spill, autovacuum, and concurrent workloads in the reserve; subtract anticipated
batch growth before deciding to proceed. Record datapoint time, minimum bytes,
reserve and forecast outside Git. A successful DB preflight alone is **not**
instance-capacity approval. No automated resize is performed. If CloudWatch
access is unavailable, obtain the current measurement from the authorized
instance owner rather than inferring it from database size.


### Parser revision changes during queue resumption

A stored receipt from an earlier parser is not a content failure. The queue
preserves a prior `ingested` status and its historical result, records
`receipt_status=stale_receipt`, excludes it from current-run verified successes,
and continues to pending work. Stale receipts do not consume the invocation's
filing-attempt limit. The run remains incomplete until a separately scoped,
versioned upgrade is verified. No old stored payload is overwritten or promoted.
For legacy v3 filings, acquire/verify a complete document set and use the reviewed
v6 profile in a separate queue; do not rerun changed parsing under old identities.
A schema prerequisite failure stops before any filing entry is modified.
