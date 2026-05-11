#requires -Version 5.1
<#
  anqp — full bootstrap from a fresh checkout, Windows / PowerShell.

  Steps:
    1. Create venv (.venv) if missing
    2. Install dependencies
    3. Install the anqp package in editable mode
    4. Run anqp bootstrap (downloads + ingests every source)
#>

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".venv")) {
    Write-Host "Creating venv at .venv ..." -ForegroundColor Cyan
    python -m venv .venv
}

$pip = ".venv\Scripts\pip.exe"
$anqp = ".venv\Scripts\anqp.exe"

Write-Host "Installing dependencies ..." -ForegroundColor Cyan
& $pip install --quiet --disable-pip-version-check --upgrade pip
& $pip install --quiet --disable-pip-version-check -r requirements.txt
& $pip install --quiet --disable-pip-version-check -e .

Write-Host "`nRunning bootstrap (this downloads ~50 MB)" -ForegroundColor Cyan
& $anqp bootstrap

Write-Host "`nDone. Start the server with:" -ForegroundColor Green
Write-Host "  .venv\Scripts\anqp.exe serve"
