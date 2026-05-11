#!/usr/bin/env bash
# Incremental update: re-fetch every source. Cache hits skip ingestion.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -d ".venv/Scripts" ]; then
    .venv/Scripts/anqp update
else
    .venv/bin/anqp update
fi
