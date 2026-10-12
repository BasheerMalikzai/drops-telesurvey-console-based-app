#!/usr/bin/env bash
# =============================================================================
# daily.sh - the morning run (scheduled by ./setup.sh --schedule).
#
#   1. Downloads new KoBo submissions and records each call result in call_list
#   2. Draws today's numbers and spreads them across the callers
#   3. Uploads assignments.csv to the KoBo form
#   4. Emails the report (or the error, if something failed)
#
# During the day, sync.sh keeps results and the callers' list up to date.
#
# Extra options are passed through, for example:
#   ./daily.sh --no-upload              # build the file but don't upload
#   ./daily.sh --force                  # redraw today's assignment
#   ./daily.sh --results-csv=export.csv # outcomes from a KoBo CSV export
#   ./daily.sh --date 2026-10-06        # run as if it were another day
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"

[[ -x "$PY" ]] || { echo "Not set up yet: run ./setup.sh first" >&2; exit 1; }
[[ -f config.json ]] || { echo "config.json missing: run ./setup.sh first" >&2; exit 1; }

# KoBo settings are read from .env by the Python code (environment variables override them)

mkdir -p data
# Locking (never at the same time as a sync) is done inside Python, through the database.

echo "---- daily run $(date '+%Y-%m-%d %H:%M:%S') ----"
exec "$PY" -m telesurvey run "$@"
