param(
    [string]$Python = "python",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$VenvPath = Join-Path $RepoRoot ".venv"

if ($Force -and (Test-Path $VenvPath)) {
    Remove-Item -Recurse -Force $VenvPath
}

if (!(Test-Path $VenvPath)) {
    & $Python -m venv $VenvPath
}

$PythonExe = Join-Path $VenvPath "Scripts\python.exe"

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Venv ready: $VenvPath"
Write-Host "Activate with:"
Write-Host ".\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Smoke check:"
Write-Host "python -m common.catalog_photo_control.bench --help"
