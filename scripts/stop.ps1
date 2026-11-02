<#
.SYNOPSIS
    Stop Nora: the bot, the model server and the tray icon.
.DESCRIPTION
    Asks her to shut down cleanly (which frees the GPU), waits, and falls back
    to killing the whole process tree if she does not respond.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
#>
param([int]$TimeoutSeconds = 25)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "data\nora.pid"
$stopFile = Join-Path $root "data\stop"

if (-not (Test-Path $pidFile)) {
    Write-Host "[nora] not running."
    exit 0
}

$supPid = [int](Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $supPid) {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "[nora] cleared a stale pid file."
    exit 0
}

if (-not (Get-Process -Id $supPid -ErrorAction SilentlyContinue)) {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "[nora] not running (stale pid file removed)."
    exit 0
}

Write-Host "[nora] asking her to shut down ..."
New-Item -ItemType File -Path $stopFile -Force | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $supPid -ErrorAction SilentlyContinue)) {
        Remove-Item $stopFile -Force -ErrorAction SilentlyContinue
        Write-Host "[nora] stopped." -ForegroundColor Green
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

Write-Host "[nora] clean shutdown timed out, killing the process tree." -ForegroundColor Yellow
& taskkill /PID $supPid /T /F | Out-Null
Remove-Item $stopFile -Force -ErrorAction SilentlyContinue
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Write-Host "[nora] stopped."
