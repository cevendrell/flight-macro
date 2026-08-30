# OvrHead daily ingest.
#
# Runs at 04:00 local. Steps:
#   1. git pull --rebase --autostash    keep repo up to date
#   2. reconstruct.py                   snapshots -> flights (per hex sessions)
#   3. sync_to_repo.py                  copy rolling window into data/adsb/
#   4. git add data/adsb + commit + push   site gets fresh data
#
# The poller itself is a separate always-running task started at logon; this
# script does not touch it.

$ErrorActionPreference = 'Stop'

$RepoRoot = 'C:\Users\docto\Documents\GitHub\flight-macro'
$Python   = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$LogDir   = Join-Path $env:USERPROFILE '.ovrhead'
$LogFile  = Join-Path $LogDir 'daily.log'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Log($msg) {
    $line = ("[{0}] {1}" -f (Get-Date -Format s), $msg)
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

Set-Location $RepoRoot
Log "=== daily ingest starting ==="

try {
    Log "git pull"
    git pull --rebase --autostash origin main 2>&1 | ForEach-Object { Log "  git: $_" }

    Log "reconstruct.py"
    & $Python (Join-Path $RepoRoot 'scripts\adsb\reconstruct.py') 2>&1 | ForEach-Object { Log "  rec: $_" }

    Log "sync_to_repo.py"
    & $Python (Join-Path $RepoRoot 'scripts\adsb\sync_to_repo.py') 2>&1 | ForEach-Object { Log "  sync: $_" }

    Log "git status"
    $changes = git status --porcelain data/adsb
    if (-not $changes) {
        Log "no data changes; skipping commit."
    } else {
        Log "committing:"
        $changes -split "`n" | ForEach-Object { Log "  $_" }
        git add data/adsb 2>&1 | ForEach-Object { Log "  add: $_" }
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm'
        git commit -m "data: refresh ADS-B snapshots ($ts UTC)" 2>&1 | ForEach-Object { Log "  commit: $_" }
        git push origin main 2>&1 | ForEach-Object { Log "  push: $_" }
    }

    Log "=== done ==="
    exit 0
}
catch {
    Log ("!! ERROR: {0}" -f $_.Exception.Message)
    exit 1
}
