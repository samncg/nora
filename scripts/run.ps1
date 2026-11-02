<#
.SYNOPSIS
    Start Nora in the background. This window is safe to close.
.DESCRIPTION
    Launches the supervisor under pythonw.exe, which has no console attached,
    so closing any terminal cannot take her down. Use -Foreground to run it
    attached to this window instead (ctrl+c stops it) while debugging.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Foreground
#>
param([switch]$Foreground)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "[nora] no virtualenv. run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
    exit 1
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile) -or -not (Select-String -Path $envFile -Pattern '^\s*DISCORD_TOKEN\s*=\s*\S' -Quiet)) {
    Write-Host "[nora] DISCORD_TOKEN is missing from .env"
    Write-Host "[nora] add your bot token (see README.md) and start me again."
    exit 1
}

if ($Foreground) {
    Write-Host "[nora] running attached to this window. ctrl+c stops her."
    & $python -m nora supervisor
    exit $LASTEXITCODE
}

$pidFile = Join-Path $root "data\nora.pid"
if (Test-Path $pidFile) {
    $existing = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
        Write-Host "[nora] already running (pid $existing). use scripts\stop.ps1 to stop her."
        exit 0
    }
}

Start-Process -FilePath $pythonw `
    -ArgumentList @("-m", "nora", "supervisor") `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Start-Sleep -Milliseconds 1200

$newPid = $null
if (Test-Path $pidFile) {
    $newPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
}

if ($newPid) {
    Write-Host "[nora] started in the background (pid $newPid)." -ForegroundColor Green
} else {
    Write-Host "[nora] starting ..." -ForegroundColor Green
}
Write-Host "[nora] this window is safe to close. she keeps running, look for the tray icon."
