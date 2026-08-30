# OvrHead daily ingest. Handles native command stderr via $LASTEXITCODE.

$ErrorActionPreference = 'Continue'

$RepoRoot = 'C:\Users\docto\Documents\GitHub\flight-macro'
$Python   = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$LogDir   = Join-Path $env:USERPROFILE '.ovrhead'
$LogFile  = Join-Path $LogDir 'daily.log'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Log($msg) {
    $line = ('[{0}] {1}' -f (Get-Date -Format s), $msg)
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

function Run {
    param([string]$Label, [string]$Exe, [string[]]$Arguments)
    Log "> $Label"
    $output = & $Exe @Arguments 2>&1 | Out-String
    foreach ($line in ($output -split "`r?`n")) {
        if ($line.Trim()) { Log ('  {0}: {1}' -f $Label, $line) }
    }
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

Set-Location $RepoRoot
Log '=== daily ingest starting ==='

try {
    Run 'git-pull'    'git'   @('pull', '--rebase', '--autostash', 'origin', 'main')
    Run 'reconstruct' $Python @((Join-Path $RepoRoot 'scripts\adsb\reconstruct.py'))
    Run 'sync'        $Python @((Join-Path $RepoRoot 'scripts\adsb\sync_to_repo.py'))

    $status = & git status --porcelain data/adsb 2>&1 | Out-String
    if (-not $status.Trim()) {
        Log 'no data changes; skipping commit.'
    } else {
        Log 'committing:'
        foreach ($line in ($status -split "`r?`n")) {
            if ($line.Trim()) { Log ('  {0}' -f $line) }
        }
        Run 'git-add'    'git' @('add', 'data/adsb')
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm'
        Run 'git-commit' 'git' @('commit', '-m', "data: refresh ADS-B snapshots ($ts UTC)")
        Run 'git-push'   'git' @('push', 'origin', 'main')
    }

    Log '=== done ==='
    exit 0
}
catch {
    Log ('!! ERROR: {0}' -f $_.Exception.Message)
    exit 1
}
