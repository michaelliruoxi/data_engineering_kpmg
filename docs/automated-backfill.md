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

The membership JSON and archived HTML must remain together. Changing the
membership snapshot, date window, or form list requires a new queue. A queue
remembers the history mapping's path and hash. An explicit discovery run may
switch to a new history file only if it preserves all earlier relationships;
keep the earlier file unchanged. Ordinary runs reject a changed mapping. A queue
lock prevents concurrent writers; after an interrupted process, confirm it has
stopped before removing its `.lock` file. Atomic checkpoint replacement uses a
short bounded retry for temporary Windows file locks.

Exit status is **0** only when the requested phase is complete, **2** for a
partial phase, **3** for blocked SEC access, and **1** for a setup/runtime error.
`--status` itself exits 0 after successfully reporting an incomplete queue.
The checkpoint's counts do not establish current database contents; live queries
and validated processing sets provide that evidence.

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
