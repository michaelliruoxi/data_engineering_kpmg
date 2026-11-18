<#
.SYNOPSIS
Run the October 13, 2026 tables-v3 backfill with several workers at once.

.DESCRIPTION
Splits the filings not attempted yet into shard queues under
data\backfill\2026-10-13\shards (one per worker, every issuer in exactly one
shard) and starts one background importer per shard. Run it again to resume:
existing shards are reused, and a shard whose worker is still running is left
alone. Each worker spaces its own SEC requests by one second, so the combined
rate is one request per second per worker; keep -Workers at 8 or fewer.

The database limits the useful worker count. Do not use this launcher while
the shared instance is a db.t4g.micro. On November 17, 2026 eight workers ran
it out of memory and four emptied its disk burst allowance; it restarted both
times. Two workers stalled it. On that class run one worker with
start_backfill.ps1 -PendingOnly, and use this launcher only after the instance
class has been raised. See "Several workers at once" in
docs\automated-backfill.md.

While shard queues exist the main queue must not be imported. When every
worker has stopped, copy the results back with:
  .\.venv\Scripts\python.exe scripts\shard_backfill_queue.py merge --state data\backfill\2026-10-13\queue.json

.EXAMPLE
.\scripts\start_parallel_backfill.ps1 -Workers 4 -MaxDatabaseGiB 40
#>
[CmdletBinding()]
param(
    [ValidateRange(2, 8)][int]$Workers = 4,
    [ValidateRange(1, 2147483647)][int]$MaxDatabaseGiB = 14
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$queueDir = Join-Path $root 'data\backfill\2026-10-13'
$queue = Join-Path $queueDir 'queue.json'
$shardDir = Join-Path $queueDir 'shards'
$python = Join-Path $root '.venv\Scripts\python.exe'

function Clear-StaleLock([string]$lock) {
    # Returns $true when the queue is free, $false when its worker is still running.
    if (-not (Test-Path $lock)) { return $true }
    $holder = (Get-Content $lock -Raw).Trim()
    if ($holder -notmatch '^\d+$') {
        throw "The queue lock does not name a process ID; inspect $lock before removing it."
    }
    if (Get-Process -Id ([int]$holder) -ErrorAction SilentlyContinue) { return $false }
    Write-Host "Removing the stale lock left by stopped process $holder."
    Remove-Item $lock
    return $true
}

if (-not $env:EDGAR_IDENTITY) {
    throw 'Set $env:EDGAR_IDENTITY to your name and contact email before starting.'
}
if (-not (Clear-StaleLock "$queue.lock")) {
    throw 'A process is still importing the main queue. Stop it before starting parallel workers.'
}

Push-Location $root
try {
    $shards = @(Get-ChildItem -Path $shardDir -Filter 'queue-*.json' -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($shards.Count -eq 0) {
        & $python 'scripts\shard_backfill_queue.py' 'split' '--state' $queue '--workers' "$Workers"
        if ($LASTEXITCODE -ne 0) { throw "Splitting the queue stopped with exit code $LASTEXITCODE." }
        $shards = @(Get-ChildItem -Path $shardDir -Filter 'queue-*.json' | Sort-Object Name)
    } else {
        Write-Host "Resuming the $($shards.Count) existing shard queues; -Workers is ignored."
    }
    & $python 'scripts\check_backfill_ready.py' '--max-database-gib' "$MaxDatabaseGiB"
    if ($LASTEXITCODE -ne 0) {
        throw "The read-only preflight stopped with exit code $LASTEXITCODE; no import was started."
    }
    $logs = Join-Path $queueDir 'logs'
    New-Item -ItemType Directory -Force $logs | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    foreach ($shard in $shards) {
        if (-not (Clear-StaleLock "$($shard.FullName).lock")) {
            Write-Host "$($shard.Name): its worker is still running; left alone."
            continue
        }
        $arguments = @(
            '-u', 'scripts\backfill_sec.py', '--ingest', '--pending-only', '--tables-version', 'tables-v3',
            '--max-database-gib', "$MaxDatabaseGiB",
            '--universe', 'data\backfill\2026-10-13\universe.json',
            '--state', "data\backfill\2026-10-13\shards\$($shard.Name)",
            '--since', '2021-10-13', '--until', '2026-10-13',
            '--env-file', '.env.aws', '--database', 'sec_filings'
        )
        $log = Join-Path $logs "backfill_${stamp}_$($shard.BaseName)"
        $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput "$log.stdout.log" -RedirectStandardError "$log.stderr.log"
        Write-Host "$($shard.Name): started process $($process.Id). Logs: $log.*"
        # Stagger the starts so the workers do not open their sessions together.
        Start-Sleep -Seconds 5
    }
} finally {
    Pop-Location
}
