<#
.SYNOPSIS
    One-shot installer: virtualenv, python deps, llama.cpp, the model, .env.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
param(
    [switch]$Cpu,
    [switch]$SkipModel,
    [switch]$SkipLlama,
    [switch]$NoVision
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "nora setup" -ForegroundColor Cyan
Write-Host "project: $root"

$python = Get-Command python -ErrorAction SilentlyContinue
$pythonArgs = @()
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
    $pythonArgs = @("-3")
}
if (-not $python) {
    throw "python 3.10+ not found on PATH. install it from python.org and rerun."
}

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "creating virtualenv ..." -ForegroundColor Yellow
    & $python.Source @pythonArgs -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "could not create virtualenv" }
}

Write-Host "installing python packages ..." -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not $SkipLlama) {
    Write-Host "installing llama.cpp server ..." -ForegroundColor Yellow
    $llamaArgs = @((Join-Path $root "scripts\fetch_llama.py"))
    if ($Cpu) { $llamaArgs += "--cpu" }
    & $venvPy @llamaArgs
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp download failed" }
}

if (-not $SkipModel) {
    Write-Host "downloading model (a few GB, grab a coffee) ..." -ForegroundColor Yellow
    $modelArgs = @((Join-Path $root "scripts\fetch_model.py"))
    if (-not $NoVision) { $modelArgs += "--mmproj" }
    & $venvPy @modelArgs
    if ($LASTEXITCODE -ne 0) { throw "model download failed" }
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "created .env - paste your discord token into it." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "setup done. next:" -ForegroundColor Green
Write-Host "  1. put DISCORD_TOKEN in .env (see README for the discord app steps)"
Write-Host "  2. try her in a terminal:  .venv\Scripts\python.exe -m nora chat"
Write-Host "  3. start her in the background:  run.bat  (or scripts\run.ps1)"
Write-Host "  4. keep it 24/7:           powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1"
Write-Host ""
Write-Host "scripts\status.ps1 shows what she is doing, scripts\stop.ps1 shuts her down."
