# OvrHead — one-off Windows install.
#
# Registers two Task Scheduler entries:
#   OvrHead Poller       runs at logon, keeps poller.py alive
#   OvrHead Daily Ingest runs at 04:00 local, reconstructs + syncs + pushes
#
# Re-run any time; it re-registers cleanly.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-tasks.ps1

$ErrorActionPreference = 'Stop'

$RepoRoot   = 'C:\Users\docto\Documents\GitHub\flight-macro'
$PollerPs1  = Join-Path $RepoRoot 'scripts\windows\ovrhead-poller.ps1'
$DailyPs1   = Join-Path $RepoRoot 'scripts\windows\ovrhead-daily.ps1'

foreach ($p in @($PollerPs1, $DailyPs1)) {
    if (-not (Test-Path $p)) { throw "missing: $p" }
}

function Register-OvrHeadTask {
    param(
        [string]$Name,
        [string]$Description,
        $Trigger,
        [string]$ScriptPath
    )
    Write-Host "-- $Name"
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "   unregistered old entry."
    }

    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $Name `
        -Description $Description `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -Principal $principal | Out-Null
    Write-Host "   registered."
}

# 1) Poller at logon.
$pollerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-OvrHeadTask `
    -Name 'OvrHead Poller' `
    -Description 'Keeps the ADS-B poller (pythonw.exe) running.' `
    -Trigger $pollerTrigger `
    -ScriptPath $PollerPs1

# 2) Daily ingest at 04:00.
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At 4am
Register-OvrHeadTask `
    -Name 'OvrHead Daily Ingest' `
    -Description 'Reconstruct flights, sync parquet to repo, git push.' `
    -Trigger $dailyTrigger `
    -ScriptPath $DailyPs1

Write-Host ""
Write-Host "Installed. Verify:"
Write-Host "  Get-ScheduledTask | Where-Object { `$_.TaskName -like 'OvrHead*' } | Format-Table TaskName, State"
Write-Host ""
Write-Host "Kick off the poller now (no need to wait for next logon):"
Write-Host "  Start-ScheduledTask -TaskName 'OvrHead Poller'"
Write-Host ""
Write-Host "Test the daily pipeline manually:"
Write-Host "  Start-ScheduledTask -TaskName 'OvrHead Daily Ingest'"
Write-Host "  Get-Content `"$env:USERPROFILE\.ovrhead\daily.log`" -Tail 40"
