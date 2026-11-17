# Reload of the two review-required filings under V8 — Windows ingest machine

Written November 16, 2026. The shared database serves two filings as
`review_required` under the October 13 `filing-import-v4` load:

| Filing | Accession | Why it is held |
| --- | --- | --- |
| Clorox 10-K, fiscal 2026 | `0000021076-26-000034` | Three cells of "Cash dividends declared and accrued, but not paid" are tagged with a balance-date concept under year columns |
| Con Edison 10-Q, June 30, 2026 (filed under Consolidated Edison Company of New York) | `0001047862-26-000142` | Two cells of "Issuance of common shares for dividend reinvestment" are tagged the same way under six-month columns |

The flagged profile (`tables-v8`) records such a tag as a company tagging error
and lets the filing pass. Both filings parse under it since the section rules
described in [reviewed-layout-upgrade.md](reviewed-layout-upgrade.md#section-layouts-accepted-on-november-16);
this page loads and publishes them. Run it in Windows PowerShell opened from the
Start menu. None of these commands has been run on Windows. Steps 5 and 7 write
to the shared database, so confirm each with the database owner first.

The load only works from the **original archived files** under
`data\raw\sec\0000021076-26-000034\` and `data\raw\sec\0001047862-26-000142\` on
the machine that registered them on October 13. SEC injects a script tag with a
random path into every HTML page it serves, so a fresh download never matches the
registered bytes. Every filing command below uses `--offline`.

## 0. Set up the terminal

```powershell
Set-Location -LiteralPath 'C:\dev\data_engineering_kpmg'
$py = '.\.venv\Scripts\python.exe'
$tables = 'tables-v8'
$scope = @('--universe','data\review-required-v8\universe.json','--state','data\review-required-v8\queue.json','--since','2026-08-06','--until','2026-08-07')
$db = @('--env-file','.env.aws','--database','sec_filings')
Get-ChildItem Env:PG*
Test-Path data\raw\sec\0000021076-26-000034\clx-20260630.htm, data\raw\sec\0001047862-26-000142\ed-20260630.htm
```

`Get-ChildItem Env:PG*` must print nothing and the last line must print `True`
twice. Re-run this block in every new terminal.

## 1. Update the code

```powershell
git status --short
git fetch origin
git switch main
git pull --ff-only
git log -1 --format="%h %s"
Test-Path docs\review-required-reload-windows.md
```

`git status --short` must print nothing before you continue; if it lists files,
stop. The last line must print `True`. No dependencies and no migrations
changed: the database stays at 001-017.

## 2. Create the two-filing universe and queue (once)

```powershell
@'
import json
from pathlib import Path
from sec_pipeline.universe import scoped_snapshot
companies = [{"ticker": "CLX", "cik": "0000021076", "name": "The Clorox Company"},
             {"cik": "0000023632", "name": "Consolidated Edison Company of New York, Inc."}]
target = Path("data/review-required-v8/universe.json")
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x") as output:
    json.dump(scoped_snapshot(companies, as_of="2026-11-16"), output, indent=2)
'@ | & $py -
$env:EDGAR_IDENTITY = Read-Host 'SEC contact as Name email'
& $py -u scripts\backfill_sec.py --discover @scope --data-root .
```

Discovery asks SEC only for the two companies' filing lists; it downloads no
filings. Expect `"filings_discovered": 2`. The two-day window holds exactly the
Con Edison 10-Q (filed August 6) and the Clorox 10-K (filed August 7).

## 3. Read-only preflight

```powershell
& $py scripts\check_backfill_ready.py --tables-version $tables @scope --max-database-gib 14
```

Expect a line starting `Read-only preflight passed: tables-v8; schema 001-017; 2 queued filings`.

## 4. Dry run from the archived originals

```powershell
& $py -u scripts\backfill_sec.py --dry-run --offline --tables-version $tables @scope --data-root .
```

Expect two `"event": "validated"` lines, Clorox with `"source_defect_cells": 3`
and Con Edison with `"source_defect_cells": 2`, both with
`"review_required_cells": 0`. A `pending` entry means that filing's archive is
not on this machine; a `failed` entry means stop and print the reason:

```powershell
(Get-Content data\review-required-v8\queue.json -Raw | ConvertFrom-Json).filings.PSObject.Properties | ForEach-Object { "$($_.Name)  $($_.Value.status)  $($_.Value.error_stage)  $($_.Value.error_detail.message)" }
```

## 5. Load (first write to the shared database)

```powershell
& $py -u scripts\backfill_sec.py --ingest --offline --validated-only --tables-version $tables @scope --data-root . @db --max-database-gib 14
```

Expect two `"event": "ingested"` lines. `Source document bytes or role changed
at the registered URL` means this machine's file is not the one registered on
October 13; nothing was written. The new `automated-v8` sets are stored next to
the `filing-import-v4` sets and are not served yet.

## 6. Audit what was loaded (read-only)

```powershell
& $py scripts\publish_reviewed_queue.py plan --state data\review-required-v8\queue.json --data-root . @db --output-dir data\review-required-v8\evidence\plan-001
```

Expect `"publish": 2` and a `plan_sha256`.

## 7. Publish (switches what readers are served)

```powershell
& $py scripts\publish_reviewed_queue.py publish --plan data\review-required-v8\evidence\plan-001\plan.json --plan-sha256 PASTE_PLAN_SHA256 --data-root . @db --output-dir data\review-required-v8\evidence\publish-001
```

Expect `"published": 2`. Retrieval indexes built on the old report and chunk IDs
of these two filings need a refresh afterwards. To withdraw the batch:

```powershell
& $py scripts\publish_reviewed_queue.py rollback --published data\review-required-v8\evidence\publish-001\publish-summary.json @db --output-dir data\review-required-v8\evidence\rollback-001
```

## 8. Check the result (read-only, any SQL client)

```sql
SELECT accession_number, set_key, validation_status, selection_reason
FROM finance.filing
WHERE accession_number IN ('0000021076-26-000034', '0001047862-26-000142');

SELECT validation_status, count(*) FROM finance.filing GROUP BY 1;
```

Expect both filings as `automated-v8`, `passed`, `explicit_corrected_preference`,
and no `review_required` row in the second result (660 `passed`).

## What was checked on the Mac, and what was not

- Both filings were downloaded again on November 16 for testing only. Their XBRL
  instance, schema and index page have the registered SHA-256; the three inline
  HTML documents are one byte longer than the registered ones (SEC's injected
  script tag), so they cannot be loaded into the shared database.
- From those copies the V8 dry run validates both filings, and a load, stored
  acceptance audit and publication into an empty local database at 001-017
  passed. The three finance statement views then return Clorox's values for all
  periods; the five flagged facts are absent from `sec.analysis_ready_facts`.
- The discovery window and steps 2 and 4 were run as written against a copy of
  those test archives.
- Not rehearsed: loading these two filings next to their existing
  `filing-import-v4` sets, which needs the original files. The shortlist load on
  November 12 did exactly this for 233 filings, one of them (NetApp
  `0001193125-26-380207`) with a `filing-import-v4` set beside it.
- Con Edison's filing is a combined report of Consolidated Edison, Inc. and its
  New York subsidiary, stored under the subsidiary, and the finance statement
  views return no rows for it before or after this load. That is a separate
  question from the review flag.
