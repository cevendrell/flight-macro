# ─────────────────────────────────────────────────────────────────────────────
#  OvrHead — one-shot Windows setup (Matebook / any Windows PC)
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
#      → writes them to %USERPROFILE%\.ovrhead\env.ps1 (user-only ACL, NEVER git)
#    - creates %USERPROFILE%\bin\ovrhead-daily.ps1 wrapper (pull-rebase safe)
#    - registers a Task Scheduler entry (04:00 UTC = 05:00 CET winter) if you want
#    - smoke-tests: pulls one day of OpenSky, one month of Eurostat
#
#  Idempotent: safe to re-run.
# ─────────────────────────────────────────────────────────────────────────────

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
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor DarkYellow
Write-Host "  OvrHead — Matebook (Windows) setup" -ForegroundColor Yellow
Write-Host "  repo:  $Repo"
Write-Host "  env:   $EnvFile"
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor DarkYellow
Write-Host ""

# ── 0. Sanity checks ────────────────────────────────────────────────
if (-not (Test-Path $Repo)) {
    Write-Host "! Repo not found at $Repo" -ForegroundColor Red
    Write-Host "  Clone it there first (via GitHub Desktop), then re-run this script."
    exit 1
}
Set-Location $Repo

# find a Python — prefer the `py` launcher, fall back to `python`
$Py = $null
foreach ($cand in @('py -3', 'python', 'python3')) {
    try {
        $ver = & cmd /c "$cand --version" 2>&1
        if ($LASTEXITCODE -eq 0) { $Py = $cand; break }
    } catch {}
}
if (-not $Py) {
    Write-Host "! Python not found." -ForegroundColor Red
    Write-Host "  Install from https://www.python.org/downloads/ (check 'Add python.exe to PATH')"
    exit 1
}
Write-Host "  ✓ Python: $($Py) — $ver"

try { $gitVer = git --version } catch {
    Write-Host "! Git not found in PATH." -ForegroundColor Red
    Write-Host "  Install Git for Windows: https://git-scm.com/download/win"
    exit 1
}
Write-Host "  ✓ $gitVer"

# ── 1. venv + deps ──────────────────────────────────────────────────
if (-not (Test-Path '.venv')) {
    Write-Host "→ Creating venv (.venv)…"
    Invoke-Expression "$Py -m venv .venv"
}
$VenvPy = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Host "! venv Python not found at $VenvPy" -ForegroundColor Red
    exit 1
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r scripts\requirements.txt
Write-Host "  ✓ deps installed"

# ── 2. warehouse dir ────────────────────────────────────────────────
foreach ($d in @($WhDir,
                 (Join-Path $WhDir 'raw\opensky'),
                 (Join-Path $WhDir 'raw\eurostat'),
                 (Join-Path $WhDir 'curated'),
                 $BinDir, $EnvDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Write-Host "  ✓ warehouse: $WhDir"

# ── 3. credentials (silent prompt, user-only ACL) ───────────────────
if (-not (Test-Path $EnvFile)) {
    New-Item -ItemType File -Path $EnvFile -Force | Out-Null
    # lock down: only the current user can read
    $acl = Get-Acl $EnvFile
    $acl.SetAccessRuleProtection($true, $false)   # break inheritance
    $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "$env:USERDOMAIN\$env:USERNAME", 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl $EnvFile $acl
}

function Get-OrPrompt {
    param([string]$Key, [string]$Prompt)
    $existing = Get-Content $EnvFile -ErrorAction SilentlyContinue |
                Where-Object { $_ -match "^\`$env:$Key\s*=" }
    if ($existing) {
        Write-Host "  ✓ $Key already set (edit $EnvFile to change)"
        return
    }
    $sec = Read-Host "  $Prompt (press Enter to skip)" -AsSecureString
    $val = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    if ($val) {
        # escape any single quotes in the value
        $safe = $val -replace "'", "''"
        Add-Content $EnvFile "`$env:$Key = '$safe'"
        Write-Host "  ✓ $Key stored"
    } else {
        Write-Host "  · $Key skipped"
    }
}

Write-Host ""
Write-Host "── OpenSky Network (sign up first: https://opensky-network.org) ──" -ForegroundColor Cyan
Get-OrPrompt 'OPENSKY_USER' 'OpenSky username'
Get-OrPrompt 'OPENSKY_PASS' 'OpenSky password (hidden)'
Write-Host ""
Write-Host "── Anthropic (optional — needed for Claude enrichment) ──" -ForegroundColor Cyan
Get-OrPrompt 'ANTHROPIC_API_KEY' 'Anthropic API key (hidden)'

# ── 4. daily wrapper ────────────────────────────────────────────────
$wrapperContent = @"
# Auto-generated by scripts\bootstrap_matebook.ps1 — safe to re-run bootstrap.
`$ErrorActionPreference = 'Continue'
`$Repo = '$Repo'
`$Log  = '$LogFile'

function Log([string]`$msg) {
    `$stamp = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')
    Add-Content -Path `$Log -Value "`$stamp `$msg"
}

# Load credentials
if (Test-Path '$EnvFile') { . '$EnvFile' }

Set-Location `$Repo

# Sync with remote first — rebase on top of anything pushed elsewhere.
`$pullOut = & git pull --rebase --autostash origin main 2>&1
if (`$LASTEXITCODE -ne 0) {
    Log "[git] pull-rebase failed:`n`$pullOut"
    exit 1
}

`$VenvPy = Join-Path `$Repo '.venv\Scripts\python.exe'

# Fetch fresh OpenSky (last 14 days). Non-fatal on failure.
`$fetchOut = & `$VenvPy scripts\fetch_opensky_now.py --days 14 2>&1
Log "[opensky]`n`$fetchOut"

# Commit + push if data/insights.json changed
& git diff --quiet data/insights.json
if (`$LASTEXITCODE -ne 0) {
    & git add data/insights.json
    & git commit -m "chore(data): daily refresh `$(Get-Date -Format 'yyyy-MM-dd')" | Out-Null
    `$pushOut = & git push origin main 2>&1
    Log "[git] pushed data update`n`$pushOut"
} else {
    Log "[ok] no data change"
}
"@
Set-Content -Path $Wrapper -Value $wrapperContent -Encoding UTF8
Write-Host "  ✓ wrapper: $Wrapper"

# ── 5. Task Scheduler ──────────────────────────────────────────────
Write-Host ""
$installTask = Read-Host "Install daily scheduled task (04:00 local time, every day)? [y/N]"
if ($installTask -match '^[Yy]') {
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
               -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Wrapper`""
    $trigger = New-ScheduledTaskTrigger -Daily -At 4am
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -MultipleInstances IgnoreNew
    # Register under current user, runs only when they're logged in (simpler; no stored password)
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "  ✓ scheduled: $TaskName  — runs daily at 04:00 local"
    Write-Host "  view:    Get-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  run now: Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  logs:    Get-Content '$LogFile' -Tail 30 -Wait"
} else {
    Write-Host "  · skipped."
    Write-Host "    Install later with:"
    Write-Host "      Register-ScheduledTask ..."
    Write-Host "    Or just run the wrapper by hand: powershell -File $Wrapper"
}

# ── 6. Smoke test ──────────────────────────────────────────────────
Write-Host ""
$smoke = Read-Host "Run a smoke test now (fetches 1 day OpenSky + 1 month Eurostat, ~4 min)? [y/N]"
if ($smoke -match '^[Yy]') {
    if (Test-Path $EnvFile) { . $EnvFile }
    if ($env:OPENSKY_USER) {
        Write-Host "→ OpenSky: yesterday, 30 hubs…"
        & $VenvPy scripts\fetch_opensky_now.py --days 1
    } else {
        Write-Host "· OpenSky creds not set — skipping OpenSky smoke test."
    }
    Write-Host "→ Eurostat: Oct 2024, 3 reporters…"
    & $VenvPy scripts\fetch_real_now.py --month 2024-10 --top 20 --top-city 40 --reporters DE,FR,ES
}

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor DarkYellow
Write-Host "  Done. What just happened:" -ForegroundColor Yellow
Write-Host "  · venv:      $Repo\.venv"
Write-Host "  · creds:     $EnvFile  (user-only ACL, never in git)"
Write-Host "  · wrapper:   $Wrapper"
Write-Host "  · warehouse: $WhDir"
Write-Host "  · logs:      Get-Content '$LogFile' -Tail 30 -Wait"
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor DarkYellow
