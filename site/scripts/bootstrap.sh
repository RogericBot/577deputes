#!/usr/bin/env bash
# anqp — full bootstrap from a fresh checkout (POSIX).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "Creating venv at .venv ..."
    python3 -m venv .venv
fi

# Detect Windows-style venv binary path under Git Bash / Cygwin.
if [ -d ".venv/Scripts" ]; then
    PIP=".venv/Scripts/pip"
    ANQP=".venv/Scripts/anqp"
else
    PIP=".venv/bin/pip"
    ANQP=".venv/bin/anqp"
fi

echo "Installing dependencies ..."
"$PIP" install --quiet --disable-pip-version-check --upgrade pip
"$PIP" install --quiet --disable-pip-version-check -r requirements.txt
"$PIP" install --quiet --disable-pip-version-check -e .

echo
echo "Running bootstrap (this downloads ~50 MB) ..."
"$ANQP" bootstrap

echo
echo "Done. Start the server with:"
echo "  $ANQP serve"
