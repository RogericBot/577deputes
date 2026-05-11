#requires -Version 5.1
# Incremental update: re-fetch every source. Cache hits skip the ingestion
# automatically (HTTP 304 / matching ETag).
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
.venv\Scripts\anqp.exe update
