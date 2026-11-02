<#
.SYNOPSIS
    Show whether Nora is running and what she has been doing lately.
#>
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "data\nora.pid"

$running = $false
if (Test-Path $pidFile) {
    $supPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $proc = if ($supPid) { Get-Process -Id $supPid -ErrorAction SilentlyContinue } else { $null }
    if ($proc) {
        $uptime = (Get-Date) - $proc.StartTime
        $hours = [int]$uptime.TotalHours
        $minutes = $uptime.Minutes
        Write-Host "[nora] running (supervisor pid $supPid, up ${hours}h ${minutes}m)" -ForegroundColor Green
        $running = $true
    } else {
        Write-Host "[nora] not running (stale pid file: $supPid)" -ForegroundColor Yellow
    }
} else {
    Write-Host "[nora] not running"
}

foreach ($name in @("supervisor.log", "nora.log")) {
    $path = Join-Path $root "data\$name"
    if (Test-Path $path) {
        Write-Host ""
        Write-Host "--- $name (last 8) ---"
        Get-Content $path -Tail 8
    }
}

if ($running) { exit 0 } else { exit 1 }
