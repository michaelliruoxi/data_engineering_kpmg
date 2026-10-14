param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$queuePath = Join-Path $projectRoot 'data\backfill\2026-10-13\queue.json'
$lockPath = "$queuePath.lock"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The project Python interpreter is missing. Restore the existing .venv before running.'
}
if (Test-Path -LiteralPath $lockPath) {
    $ownerPid = (Get-Content -LiteralPath $lockPath -Raw).Trim()
    throw "The queue is locked by PID $ownerPid. This launcher never removes a lock or starts a second writer."
}
if (-not $CheckOnly -and [string]::IsNullOrWhiteSpace($env:EDGAR_IDENTITY)) {
    throw 'Set EDGAR_IDENTITY to your approved contact name and email in this terminal.'
}

$logDirectory = Join-Path $projectRoot 'data\backfill\2026-10-13\logs'
$temporaryDirectory = Join-Path $projectRoot '.cache\import-temp'
New-Item -ItemType Directory -Force -Path $logDirectory, $temporaryDirectory | Out-Null
$env:TEMP = $temporaryDirectory
$env:TMP = $temporaryDirectory
$env:PYTHONUNBUFFERED = '1'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$prefix = Join-Path $logDirectory "backfill_${stamp}_$PID"
$outputLog = "$prefix.stdout.log"
$errorLog = "$prefix.stderr.log"
$resultLog = "$prefix.result.txt"
$runExitCode = $null
$phase = 'preflight'

Write-Host "Output log: $outputLog"
Write-Host "Error log:  $errorLog"
try {
    # Native stderr must be logged without PowerShell 5 treating it as a terminating error.
    $ErrorActionPreference = 'Continue'
    & $pythonPath -u (Join-Path $PSScriptRoot 'check_backfill_ready.py') 2> $errorLog |
        Tee-Object -FilePath $outputLog
    $preflightExitCode = $LASTEXITCODE
    if ($preflightExitCode -ne 0) {
        $runExitCode = $preflightExitCode
    } elseif ($CheckOnly) {
        $runExitCode = 0
    } else {
        $phase = 'import'
        & $pythonPath -u (Join-Path $PSScriptRoot 'backfill_sec.py') `
            --ingest --tables-version tables-v3 --max-database-gib 14 `
            --universe data/backfill/2026-10-13/universe.json `
            --state data/backfill/2026-10-13/queue.json `
            --since 2021-10-13 --until 2026-10-13 `
            --data-root . --env-file .env.aws --database sec_filings 2>> $errorLog |
            Tee-Object -FilePath $outputLog -Append
        $runExitCode = $LASTEXITCODE
    }
} finally {
    $ErrorActionPreference = 'Stop'
    $result = if ($null -eq $runExitCode) { 'interrupted' } else { "exit_code=$runExitCode" }
    "finished_at=$(Get-Date -Format o)`nphase=$phase`n$result" | Set-Content -LiteralPath $resultLog -Encoding UTF8
    Write-Host "Run $result. Check $resultLog and both logs."
}
if ($null -ne $runExitCode) { exit $runExitCode }
