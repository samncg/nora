<#
.SYNOPSIS
    Register a scheduled task so Nora starts with Windows and survives crashes.
.DESCRIPTION
    Default trigger is "when you log on", which needs no admin rights.
    Use -AtStartup (run as administrator) to also start before anyone logs in.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -AtStartup
#>
param(
    [switch]$AtStartup,
    [string]$TaskName = "NoraAI"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    throw "no virtualenv yet. run scripts\setup.ps1 first."
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "-m nora supervisor" `
    -WorkingDirectory $root

$triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
if ($AtStartup) {
    $triggers += New-ScheduledTaskTrigger -AtStartup
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "Nora: local tomboy girlfriend bot (llama.cpp + discord)" `
    -Force | Out-Null

Write-Host "registered scheduled task '$TaskName'." -ForegroundColor Green
if ($AtStartup) {
    Write-Host "it will also start at boot (needs the account to be able to run without login)."
}
Write-Host ""
Write-Host "start now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "check:       powershell -ExecutionPolicy Bypass -File scripts\status.ps1"
Write-Host "stop:        powershell -ExecutionPolicy Bypass -File scripts\stop.ps1"
Write-Host "remove:      powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1"
