# Reviewed-profile load of the 233 shortlist filings — Windows ingest machine

Written November 11, 2026 for `main` with migration 016 (the audited V7 profile). Run in
Windows PowerShell opened from the Start menu. None of these commands has been run on
Windows yet; steps 3, 7, 8 and 10 write to the shared database, so confirm each with the
database owner before running it.

The load only works from the **original archived files** under `data\raw\sec\` on the
machine that first registered them. SEC injects a script tag with a random path into every
HTML page it serves, so a fresh download never matches the bytes the shared database
registered. Never let this run download filings again: every filing command below uses
`--offline`.

## 0. Set up the terminal

```powershell
Set-Location -LiteralPath 'C:\dev\data_engineering_kpmg'
$py = '.\.venv\Scripts\python.exe'
$tables = 'tables-v6'   # or 'tables-v7' for the audited profile; decide before step 5
$scope = @('--universe','data\shortlist-v6\universe.json','--state','data\shortlist-v6\queue.json','--since','2021-01-01','--until','2026-10-29')
$db = @('--env-file','.env.aws','--database','sec_filings')
Get-ChildItem Env:PG*
```

The last line must print nothing. Re-run this block in every new terminal.

`tables-v6` (reviewed profile) and `tables-v7` (audited profile) accept the same 227
shortlist filings with identical facts, cells and section boundaries. V7 also corrects
section titles, proxy-incorporation flags, section types and footnote-marker spacing, and
inventories omitted statement tables; see
[reviewed-layout-upgrade.md](reviewed-layout-upgrade.md#audited-profile-v7). Use one
profile for the whole queue.

## 1. Update the code

```powershell
git status --short
git fetch origin
git switch main
git pull --ff-only
git log -1 --format="%h %s"
Test-Path src\sec_pipeline\migrations\016_audited_layout_versions.sql
```

`git status --short` must print nothing before you continue; if it lists files, stop.
The last line must print `True`. No dependencies changed, so the existing `.venv` is fine.

## 2. Read-only readiness report

```powershell
& $py scripts\check_upgrade_ready.py @db --output data\shortlist-v6\evidence\before-migration.json
```

Expect `"status": "ready_to_migrate"` with pending migrations 011 through 016.

## 3. Apply migrations 011-016 (first write to the shared database)

A backup taken at 18:01 UTC on November 11 is on the Mac:
`backups/sec_filings-rds-before-011-20261111T180128Z.dump`. Take a new one if anything
has been loaded since.

```powershell
& $py -m sec_pipeline.db_cli --env-file .env.aws migrate
& $py scripts\check_upgrade_ready.py @db --output data\shortlist-v6\evidence\after-migration.json
```

Expect `"applied"` to list the six migrations, then `"status": "current"`. Known reader
effects, all from 011-015: 127 chunks of the unvalidated NetApp filing
`0000950170-24-023115` leave `sec.chunk_citations` (still in
`sec.chunk_citations_history`), and three `finance.income_statement` cells print 11.80 as
11.8. Migration 016 adds five rows to `sec.section_types` and changes no other row or view.

## 4. Create the shortlist universe and queue (once)

```powershell
@'
import json
from pathlib import Path
from sec_pipeline.universe import scoped_snapshot
config = json.loads(Path("config/shortlist.json").read_text())
target = Path("data/shortlist-v6/universe.json")
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x") as output:
    json.dump(scoped_snapshot(config["companies"], as_of="2026-11-10"), output, indent=2)
'@ | & $py -
$env:EDGAR_IDENTITY = Read-Host 'SEC contact as Name email'
& $py -u scripts\backfill_sec.py --discover @scope --data-root .
```

Discovery asks SEC only for the ten companies' filing lists; it downloads no filings.
Expect `"filings_discovered": 233`. Discovery now also lists `coverage_warnings`
(amendments and other forms it does not queue, reporting-period gaps); they change no
status.

## 5. Read-only preflight

```powershell
& $py scripts\check_backfill_ready.py --tables-version $tables @scope --max-database-gib 14
```

Expect `Read-only preflight passed: tables-v6; schema 001-015; 233 queued filings`
(`tables-v7; schema 001-016` for the audited profile).

## 6. Dry run from the archived originals (about 30 minutes)

```powershell
& $py -u scripts\backfill_sec.py --dry-run --offline --tables-version $tables @scope --data-root .
```

Expect `validated` 225, `validated_review_required` 2, `failed` 6. Any `pending` entry
means that filing's archive is not on this machine. Exit code 2 is normal.

## 7. One-filing canary

```powershell
& $py -u scripts\backfill_sec.py --ingest --offline --validated-only --tables-version $tables @scope --data-root . @db --max-database-gib 14 --limit 1
```

Expect one line with `"event": "ingested"`. If it says `"failed"`, stop and print the reason:

```powershell
(Get-Content data\shortlist-v6\queue.json -Raw | ConvertFrom-Json).filings.PSObject.Properties | Where-Object { $_.Value.status -eq 'failed' } | ForEach-Object { "$($_.Name)  $($_.Value.error_stage)  $($_.Value.error_detail.message)" }
```

`Source document bytes or role changed at the registered URL` means this machine's file is
not the one registered in the shared database for that filing. Nothing was written.

## 8. Load the rest (about an hour, adds about 1.7 GiB)

```powershell
& $py -u scripts\backfill_sec.py --ingest --offline --validated-only --tables-version $tables @scope --data-root . @db --max-database-gib 14
```

Expect 225 `ingested` and 2 `ingested_review_required` in total. Stop with Ctrl+C once;
run the same command to resume.

## 9. Audit what was loaded (read-only)

```powershell
& $py scripts\publish_reviewed_queue.py plan --state data\shortlist-v6\queue.json --data-root . @db --output-dir data\shortlist-v6\evidence\plan-001
```

Expect `"publish": 225` and a `plan_sha256`. Stop here until teammates who read the
database know the next step is coming.

## 10. Publish (switches what readers are served)

```powershell
& $py scripts\publish_reviewed_queue.py publish --plan data\shortlist-v6\evidence\plan-001\plan.json --plan-sha256 PASTE_PLAN_SHA256 --data-root . @db --output-dir data\shortlist-v6\evidence\publish-001
```

Expect `"published": 225`. Retrieval indexes built on the old report and chunk IDs need a
refresh afterwards. To withdraw the batch:

```powershell
& $py scripts\publish_reviewed_queue.py rollback --published data\shortlist-v6\evidence\publish-001\publish-summary.json @db --output-dir data\shortlist-v6\evidence\rollback-001
```

## What was checked on the Mac, and what was not

- Migrations 011-015 were applied to a restored copy of the shared database: 25 of 26
  tables byte-identical, the two reader effects listed in step 3.
- Migration 016 was then applied to a clone of that migrated copy: all 37 views and 26 of
  28 tables identical (2.16 million rows fingerprinted); only the migration ledger and
  `sec.section_types` (five new types) changed. The seven redefined trigger and validation
  functions only add the V7 identities; the V5 and V6 branches keep their pinned revisions.
- The 290,632 facts already stored for these 233 filings agree exactly with the merged
  parser, so the load will reuse them.
- A fresh database load of all 233 gave 225 passed, 2 review required, 6 rejected for
  defects in the filer's own tagging. V7 accepts the same 227 with identical facts and cells.
- Not rehearsed: storing the new reports, chunks and tables next to the existing ones.
  The canary in step 7 is the first real test of that.
