# =============================================================================
#  OvrHead -- one-shot Windows setup (Matebook / any Windows PC)
#
#  Run once from PowerShell after cloning the repo:
#      cd $HOME\Documents\GitHub\flight-macro
#      powershell -ExecutionPolicy Bypass -File scripts\bootstrap_matebook.ps1
#
#  Does:
#    - checks Python + git are available
#    - creates a Python venv + installs deps
#    - creates %USERPROFILE%\data\ovrhead-warehouse\
#    - prompts (silently) for OpenSky + Anthropic credentials
#      -> writes them to %USERPROFILE%\.ovrhead\env.ps1 (user-only ACL, NEVER git)
#    - creates %USERPROFILE%\bin\ovrhead-daily.ps1 wrapper (pull-rebase safe)
#    - registers a Task Scheduler entry (04:00 local) if you want
#    - smoke-tests: pulls one day of OpenSky, one month of Eurostat
#
#  Idempotent: safe to re-run.
# =============================================================================

$ErrorActionPreference = 'Stop'

$Repo    = if ($env:REPO) { $env:REPO } else { Join-Path $HOME 'Documents\GitHub\flight-macro' }
$EnvDir  = Join-Path $HOME '.ovrhead'
$EnvFile = Join-Path $EnvDir 'env.ps1'
$BinDir  = Join-Path $HOME 'bin'
$Wrapper = Join-Path $BinDir 'ovrhead-daily.ps1'
$WhDir   = Join-Path $HOME 'data\ovrhead-warehouse'
$LogFile = Join-Path $WhDir 'pipeline.log'
$TaskName = 'OvrHead Daily Ingest'

Write-Host ""
Write-Host "==============================================================="
Write-Host "  OvrHead -- Matebook (Windows) setup"
Write-Host "  repo:  $Repo"
Write-Host "  env:   $EnvFile"
Write-Host "==============================================================="
Write-Host ""

# ---- 0. Sanity checks --------------------------------------------------------
if (-not (Test-Path $Repo)) {
    Write-Host "! Repo not found at $Repo"
    Write-Host "  Clone it there first (via GitHub Desktop), then re-run this script."
    exit 1
}
Set-Location $Repo

# Prefer the `py` launcher; fall back to `python`, then `python3`.
$Py = $null
foreach ($cand in @('py','python','python3')) {
    $found = Get-Command $cand -ErrorAction SilentlyContinue
    if ($found) { $Py = $found.Source; break }
}
if (-not $Py) {
    Write-Host "! Python not found in PATH."
    Write-Host "  Install from https://www.python.org/downloads/ (check 'Add python.exe to PATH')"
    exit 1
}
$pyVer = & $Py --version 2>&1
Write-Host "  [OK] Python: $Py -- $pyVer"

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    Write-Host "! Git not found in PATH."
    Write-Host "  Install Git for Windows: https://git-scm.com/download/win"
    exit 1
}
$gitVer = & git --version
Write-Host "  [OK] $gitVer"

# ---- 1. venv + deps ---------------------------------------------------------
if (-not (Test-Path '.venv')) {
    Write-Host "-> Creating venv (.venv)..."
    & $Py -m venv .venv
}
$VenvPy = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Host "! venv Python not found at $VenvPy"
    exit 1
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r scripts\requirements.txt
Write-Host "  [OK] deps installed"

# ---- 2. warehouse dirs ------------------------------------------------------
foreach ($d in @($WhDir,
                 (Join-Path $WhDir 'raw\opensky'),
                 (Join-Path $WhDir 'raw\eurostat'),
                 (Join-Path $WhDir 'curated'),
                 $BinDir, $EnvDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Write-Host "  [OK] warehouse: $WhDir"

# ---- 3. credentials (silent, user-only ACL) --------------------------------
if (-not (Test-Path $EnvFile)) {
    New-Item -ItemType File -Path $EnvFile -Force | Out-Null
    try {
        $acl = Get-Acl $EnvFile
        $acl.SetAccessRuleProtection($true, $false)
        $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            "$env:USERDOMAIN\$env:USERNAME", 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl $EnvFile $acl
    } catch {
        Write-Host "  ! could not lock ACL on env file; continuing"
    }
}

function Get-OrPrompt {
    param([string]$Key, [string]$PromptText)
    $existing = Get-Content $EnvFile -ErrorAction SilentlyContinue |
                Where-Object { $_ -match ('^\$env:' + [regex]::Escape($Key) + '\s*=') }
    if ($existing) {
        Write-Host "  [OK] $Key already set (edit $EnvFile to change)"
        return
    }
    $sec = Read-Host "  $PromptText (press Enter to skip)" -AsSecureString
    $val = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    if ($val) {
        $safe = $val -replace "'", "''"
        Add-Content $EnvFile "`$env:$Key = '$safe'"
        Write-Host "  [OK] $Key stored"
    } else {
        Write-Host "  [--] $Key skipped"
    }
}

# Also record repo path so the wrapper knows where to cd
$repoLine = "`$env:OVRHEAD_REPO = '$Repo'"
$existingRepo = Get-Content $EnvFile -ErrorAction SilentlyContinue |
                Where-Object { $_ -match '^\$env:OVRHEAD_REPO\s*=' }
if (-not $existingRepo) { Add-Content $EnvFile $repoLine }

Write-Host ""
Write-Host "-- OpenSky Network (sign up first: https://opensky-network.org) --"
Get-OrPrompt 'OPENSKY_USER' 'OpenSky username'
Get-OrPrompt 'OPENSKY_PASS' 'OpenSky password (hidden)'
Write-Host ""
Write-Host "-- Anthropic (optional -- needed for Claude enrichment) --"
Get-OrPrompt 'ANTHROPIC_API_KEY' 'Anthropic API key (hidden)'

# ---- 4. daily wrapper (single-quoted here-string = LITERAL, no escaping) ----
$wrapperContent = @'
# Auto-generated by scripts\bootstrap_matebook.ps1 -- safe to re-run bootstrap.
$ErrorActionPreference = 'Continue'

$EnvFile = Join-Path $HOME '.ovrhead\env.ps1'
$LogFile = Join-Path $HOME 'data\ovrhead-warehouse\pipeline.log'

function Log([string]$msg) {
    $stamp = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')
    Add-Content -Path $LogFile -Value "$stamp $msg"
}

# Load credentials
if (Test-Path $EnvFile) { . $EnvFile }
if (-not $env:OVRHEAD_REPO) {
    $env:OVRHEAD_REPO = Join-Path $HOME 'Documents\GitHub\flight-macro'
}
Set-Location $env:OVRHEAD_REPO

# Sync with remote first -- rebase on top of anything pushed elsewhere.
$pullOut = & git pull --rebase --autostash origin main 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "[git] pull-rebase failed:`n$pullOut"
    exit 1
}

$VenvPy = Join-Path $env:OVRHEAD_REPO '.venv\Scripts\python.exe'

# Fetch fresh OpenSky (last 14 days). Non-fatal on failure.
$fetchOut = & $VenvPy scripts\fetch_opensky_now.py --days 14 2>&1
Log "[opensky]`n$fetchOut"

# Commit + push if data/insights.json changed
& git diff --quiet data/insights.json
if ($LASTEXITCODE -ne 0) {
    & git add data/insights.json
    & git commit -m "chore(data): daily refresh $(Get-Date -Format 'yyyy-MM-dd')" | Out-Null
    $pushOut = & git push origin main 2>&1
    Log "[git] pushed data update`n$pushOut"
} else {
    Log "[ok] no data change"
}
'@
Set-Content -Path $Wrapper -Value $wrapperContent -Encoding UTF8
Write-Host "  [OK] wrapper: $Wrapper"

# ---- 5. Task Scheduler ------------------------------------------------------
Write-Host ""
$installTask = Read-Host "Install daily scheduled task (04:00 local time, every day)? [y/N]"
if ($installTask -match '^[Yy]') {
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
               -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Wrapper`""
    $trigger = New-ScheduledTaskTrigger -Daily -At 4am
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "  [OK] scheduled: $TaskName -- runs daily at 04:00 local"
    Write-Host "  view:    Get-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  run now: Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  logs:    Get-Content '$LogFile' -Tail 30 -Wait"
} else {
    Write-Host "  [--] skipped. Trigger manually with:"
    Write-Host "       powershell -File $Wrapper"
}

# ---- 6. Smoke test ---------------------------------------------------------
Write-Host ""
$smoke = Read-Host "Run a smoke test now (1 day OpenSky + 1 month Eurostat, ~4 min)? [y/N]"
if ($smoke -match '^[Yy]') {
    if (Test-Path $EnvFile) { . $EnvFile }
    if ($env:OPENSKY_USER) {
        Write-Host "-> OpenSky: yesterday, 30 hubs..."
        & $VenvPy scripts\fetch_opensky_now.py --days 1
    } else {
        Write-Host "[--] OpenSky creds not set -- skipping OpenSky smoke test."
    }
    Write-Host "-> Eurostat: Oct 2024, 3 reporters..."
    & $VenvPy scripts\fetch_real_now.py --month 2024-10 --top 20 --top-city 40 --reporters DE,FR,ES
}

Write-Host ""
Write-Host "==============================================================="
Write-Host "  Done. What just happened:"
Write-Host "  - venv:      $Repo\.venv"
Write-Host "  - creds:     $EnvFile  (user-only ACL, never in git)"
Write-Host "  - wrapper:   $Wrapper"
Write-Host "  - warehouse: $WhDir"
Write-Host "  - logs:      Get-Content '$LogFile' -Tail 30 -Wait"
Write-Host "==============================================================="
