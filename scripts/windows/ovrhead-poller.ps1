# OvrHead poller launcher.
#
# Started at Windows logon by Task Scheduler. Keeps a single background
# pythonw.exe process running poller.py against the local tar1090.
# Idempotent: if a poller is already running, does nothing.

$ErrorActionPreference = 'Stop'

$RepoRoot = 'C:\Users\docto\Documents\GitHub\flight-macro'
$Python   = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
$Script   = Join-Path $RepoRoot 'scripts\adsb\poller.py'
$LogDir   = Join-Path $env:USERPROFILE '.ovrhead'
$LogFile  = Join-Path $LogDir 'poller.log'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Is a poller process already alive?
$existing = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'poller\.py' }

if ($existing) {
    Add-Content -Path $LogFile -Value ("[{0}] already running (PID {1}); nothing to do." -f (Get-Date -Format s), $existing.ProcessId)
    exit 0
}

Set-Location $RepoRoot
$proc = Start-Process -FilePath $Python -ArgumentList "`"$Script`"" -WindowStyle Hidden -PassThru
Add-Content -Path $LogFile -Value ("[{0}] started poller PID {1}" -f (Get-Date -Format s), $proc.Id)
