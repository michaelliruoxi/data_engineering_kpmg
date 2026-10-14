<#
.SYNOPSIS
Resume the October 13, 2026 tables-v3 backfill in the background, with logs.

.DESCRIPTION
Refuses to start while the process named in queue.json.lock is still running;
removes the lock only when that process has stopped. Runs the read-only
preflight (schema, privileges, size) before importing. Output goes to
data\backfill\2026-10-13\logs\backfill_<timestamp>.{stdout,stderr}.log.
Set $env:EDGAR_IDENTITY to your name and contact email first.

.EXAMPLE
.\scripts\start_backfill.ps1 -RetryFailedOnly   # first load facts/text for earlier failures
.\scripts\start_backfill.ps1                    # then continue the pending queue
#>
param(
    [switch]$RetryFailedOnly,
    [ValidateRange(1, 100)][int]$MaxDatabaseGiB = 14
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$queueDir = Join-Path $root 'data\backfill\2026-10-13'
$lock = Join-Path $queueDir 'queue.json.lock'
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not $env:EDGAR_IDENTITY) {
    throw 'Set $env:EDGAR_IDENTITY to your name and contact email before starting.'
}
if (Test-Path $lock) {
    $holder = (Get-Content $lock -Raw).Trim()
    if ($holder -notmatch '^\d+$') {
        throw "The queue lock does not name a process ID; inspect $lock before removing it."
    }
    if (Get-Process -Id ([int]$holder) -ErrorAction SilentlyContinue) {
        throw "Process $holder is still running and holds the queue lock."
    }
    Write-Host "Removing the stale lock left by stopped process $holder."
    Remove-Item $lock
}

Push-Location $root
try {
    & $python 'scripts\check_backfill_ready.py'
    if ($LASTEXITCODE -ne 0) {
        throw "The read-only preflight stopped with exit code $LASTEXITCODE; no import was started."
    }
    $logs = Join-Path $queueDir 'logs'
    New-Item -ItemType Directory -Force $logs | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $arguments = @(
        'scripts\backfill_sec.py', '--ingest', '--tables-version', 'tables-v3',
        '--max-database-gib', "$MaxDatabaseGiB",
        '--universe', 'data\backfill\2026-10-13\universe.json',
        '--state', 'data\backfill\2026-10-13\queue.json',
        '--since', '2021-10-13', '--until', '2026-10-13',
        '--env-file', '.env.aws', '--database', 'sec_filings'
    )
    if ($RetryFailedOnly) { $arguments += '--retry-failed-only' }
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "backfill_$stamp.stdout.log") `
        -RedirectStandardError (Join-Path $logs "backfill_$stamp.stderr.log")
    Write-Host "Started backfill process $($process.Id). Logs: $logs\backfill_$stamp.*"
    Write-Host 'Check progress with scripts\backfill_sec.py --status (see readme.md).'
} finally {
    Pop-Location
}
