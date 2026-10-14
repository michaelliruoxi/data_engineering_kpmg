# Terminal backfill handoff

Prepared October 13, 2026. The two active import processes were stopped, their
database transactions ended, and the queue was backed up before removing the
stopped worker's lock (PID 21952). Completed database commits and source archives
are preserved. No import was started for this handoff.

Use the established **tables-v3** parser with **facts-v1**, **report-v1**, and
**paragraph-v1**. Database migrations 001–009 are already applied. No migration
or dependency installation is part of these commands. Newer parser work and
previously imported V4 data remain preserved, but this launcher does not select
the evolving V4 parser.

The handoff was checked with a read-only database preflight, one cached V3 filing
(1,272 facts, four tables, one report, 155 chunks; all 18 checks passed), and four
focused infrastructure-stop/unsupported-filing checks. No broad test run or new
ingestion was performed for the handoff.

## Launch the complete existing queue

Open Windows Terminal or PowerShell **from the Windows Start menu**, outside any
application-hosted terminal. Paste:

```powershell
Set-Location -LiteralPath 'C:\dev\data_engineering_kpmg'
$env:EDGAR_IDENTITY = Read-Host 'Enter your approved SEC contact as Name email'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_backfill.ps1
```

Use the contact name and email already approved for SEC requests. This is a
runtime environment variable, not a database password. The script reads database
credentials from the existing private `.env.aws`; do not print or paste that
file. The TLS configuration and referenced CA certificate must remain available.
The launcher uses the existing `.venv\Scripts\python.exe` (Python 3.12).

The launcher runs this importer command after a read-only preflight:

```powershell
.\.venv\Scripts\python.exe -u .\scripts\backfill_sec.py `
  --ingest --tables-version tables-v3 --max-database-gib 14 `
  --universe data/backfill/2026-10-13/universe.json `
  --state data/backfill/2026-10-13/queue.json `
  --since 2021-10-13 --until 2026-10-13 `
  --data-root . --env-file .env.aws --database sec_filings
```

There is **no filing limit**. The existing queue contains 9,833 discovered filings
and remembers the verified predecessor mapping. Keep the queue, membership JSON
and HTML, predecessor configuration/evidence, and archived source files together.
Only run one writer for this queue. The launcher refuses an existing lock and
never deletes locks automatically.

Timestamped `.stdout.log`, `.stderr.log`, and `.result.txt` files are written to
`data\backfill\2026-10-13\logs`. The console prints their exact paths. Output is
also shown in the launch terminal. No AI service, agent, reminder, or scheduled
monitor is required.

## Progress and failed filings

In a second independent terminal:

```powershell
Set-Location -LiteralPath 'C:\dev\data_engineering_kpmg'
.\.venv\Scripts\python.exe .\scripts\backfill_sec.py --status `
  --universe data/backfill/2026-10-13/universe.json `
  --state data/backfill/2026-10-13/queue.json `
  --since 2021-10-13 --until 2026-10-13
```

This is read-only and works while the importer owns the lock. Status describes
the queue and selected parser; it is not an independent count of database rows.
A profile change can cause revalidation. Previously stored versions are retained.

```powershell
# List log files, or watch the stdout path printed by the launcher.
Get-ChildItem .\data\backfill\2026-10-13\logs | Sort-Object LastWriteTime -Descending
$latestLog = Get-ChildItem .\data\backfill\2026-10-13\logs\*.stdout.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -LiteralPath $latestLog.FullName -Tail 20 -Wait
# Ctrl+C here stops only this log-view command.
```

```powershell
$queue = Get-Content .\data\backfill\2026-10-13\queue.json -Raw | ConvertFrom-Json
$queue.filings.PSObject.Properties |
  Where-Object { $_.Value.status -in @('failed','access_blocked','ingested_review_required','validated_review_required') } |
  ForEach-Object {
    [pscustomobject]@{
      Accession = $_.Name
      Status = $_.Value.status
      ErrorType = $_.Value.error_type
      Stage = $_.Value.error_stage
      Attempts = $_.Value.attempts
    }
  } | Format-Table -AutoSize -Wrap
```

## Stop and resume

Press **Ctrl+C once in the import terminal**, then wait for the command prompt.
The active transaction is rolled back if interrupted before commit; completed
filings remain committed. If interruption occurs between commit and checkpoint
replacement, the next run verifies the sealed database receipt before resuming.
Do not use forced termination as the normal stop method.

To resume, run the same launch block again. Set `EDGAR_IDENTITY` again if using a
new terminal. Do not add `--replay`, remove source files, reset the queue, or
start a second writer.

If an abnormal termination leaves a lock, first inspect its owner:

```powershell
$lockPath = 'C:\dev\data_engineering_kpmg\data\backfill\2026-10-13\queue.json.lock'
if (Test-Path -LiteralPath $lockPath) {
  $ownerPid = [int](Get-Content -LiteralPath $lockPath -Raw).Trim()
  Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" |
    Select-Object ProcessId, Name, CreationDate, CommandLine
  Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
}
```

Never remove the lock while that process is running. If the previous import has
definitively exited and both checks show no process, remove only that stale lock:

```powershell
# Run only after the owner is confirmed stopped. The launcher does not do this.
if (Test-Path -LiteralPath $lockPath) {
  $ownerPid = [int](Get-Content -LiteralPath $lockPath -Raw).Trim()
  if (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue) {
    throw 'The lock owner still exists; leave the lock in place.'
  }
  Remove-Item -LiteralPath $lockPath
}
```

If process inspection is denied or ambiguous, leave the lock in place. Process
IDs can be reused; do not terminate an unrelated process with the same number.

## Prerequisites and limits

- Keep Windows awake, the computer powered, the launch terminal open, and the
  network connected. RDS must permit this computer's network address. Locking
  the screen is fine if sleep is disabled. Closing the application used for the
  earlier development does not affect a terminal opened independently from Start;
  closing that terminal, sleeping, rebooting, or signing out interrupts the run.
- Unsupported source/layout/validation cases are saved as failures while other
  supported filings continue. V3 does not handle every multi-document filing or
  source-period conflict. It never invents missing financial values. Existing V4
  data is retained even when this profile records an unsupported filing.
- SEC requests retain at least one second spacing, TLS, bounded transient retries,
  and access-block detection. Access blocks stop the run; resolve their cause
  before resuming. Serious connection, permission, schema, storage, or transport
  errors now stop with the checkpoint preserved instead of failing the whole queue
  one filing at a time. No automatic retry loop is installed.
- The cutoff is **14 GiB for this database**, checked before the next import.
  One filing can cross it; it does not measure instance-wide free space or WAL.
  No RDS storage setting was changed for the handoff. Full history may need more
  capacity. The importer stops rather than resizing it.
- Full cleaned text and source-detected sections are stored. Section labels and
  browser-visible completeness are not certified. Statement coverage is primary
  statements, not every note table. Scope excludes amendments and historical
  index membership changes.
- Exit codes: `0` complete requested phase; `2` incomplete phase or size cutoff;
  `3` SEC access blocked; `1` setup/infrastructure error; `130` keyboard stop.
  The read-only `--status` command exits `0` even when the queue is incomplete.
  A halted PowerShell pipeline may instead record `interrupted` in the result file.

No recurring schedule is installed. This is a manual full-queue run and resume
workflow. Preserve the current environment and code while a run is active.
