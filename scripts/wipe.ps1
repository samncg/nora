<#
.SYNOPSIS
    Erase everything Nora remembers and start her again.
.DESCRIPTION
    Stops the bot, deletes every per-person database in data\people plus the
    terminal fallback data\nora.db, then starts her again with an empty memory.
    The logs are kept.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\wipe.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\wipe.ps1 -NoRestart
#>
param([switch]$NoRestart)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$data = Join-Path $root "data"
$people = Join-Path $data "people"
$pidFile = Join-Path $data "nora.pid"
$patterns = @("*.db", "*.db-wal", "*.db-shm", "*.db-journal")

$supPid = $null
if (Test-Path $pidFile) {
    $supPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
}
$wasRunning = [bool]($supPid -and (Get-Process -Id $supPid -ErrorAction SilentlyContinue))

Write-Host "[nora] stopping her before touching memory ..."
& (Join-Path $PSScriptRoot "stop.ps1")

if (-not (Test-Path $people)) {
    New-Item -ItemType Directory -Path $people -Force | Out-Null
}

$targets = @()
foreach ($pattern in $patterns) {
    $targets += Get-ChildItem -Path (Join-Path $people $pattern) -File -ErrorAction SilentlyContinue
    $target = Join-Path $data $pattern
    if (Test-Path $target) { $targets += Get-Item $target }
}

if ($targets.Count -eq 0) {
    Write-Host "[nora] memory is already empty."
} else {
    foreach ($file in $targets) {
        $done = $false
        for ($attempt = 0; $attempt -lt 3 -and -not $done; $attempt++) {
            try {
                Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
                $done = $true
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }
        if ($done) {
            Write-Host "[nora] deleted $($file.FullName)"
        } else {
            Write-Host "[nora] could not delete $($file.FullName) - is she still running?" -ForegroundColor Yellow
            exit 1
        }
    }
}

if ($NoRestart) {
    Write-Host "[nora] memory wiped. she is stopped." -ForegroundColor Green
    exit 0
}

if ($wasRunning) {
    Write-Host "[nora] starting her again with a clean memory ..."
    & (Join-Path $PSScriptRoot "run.ps1")
} else {
    Write-Host "[nora] memory wiped. start her with run.bat when you are ready." -ForegroundColor Green
}
