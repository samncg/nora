<#
.SYNOPSIS
    Remove the Nora autostart task and stop her if she is running.
#>
param([string]$TaskName = "NoraAI")

$ErrorActionPreference = "Continue"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "no scheduled task named '$TaskName'."
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "removed '$TaskName'." -ForegroundColor Green
