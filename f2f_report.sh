#!/usr/bin/env bash
# =============================================================================
# f2f_report.sh - email the face-to-face survey's daily progress report.
#
# Reads the face-to-face KoBo form set in config.json ("face_to_face") and emails,
# per surveyor: interviews on each day up to the report day (yesterday by default)
# and the total. Changes nothing in KoBo or the database.
#
#   ./f2f_report.sh                     # email yesterday's report
#   ./f2f_report.sh --print             # show it here instead of emailing
#   ./f2f_report.sh --day 2026-10-27    # report on another day
#
# Schedule it once a day (PythonAnywhere: Tasks tab, e.g. 22:40 UTC = 03:10 Kabul).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"

[[ -x "$PY" ]] || { echo "Not set up yet: run ./setup.sh first" >&2; exit 1; }
[[ -f config.json ]] || { echo "config.json missing: run ./setup.sh first" >&2; exit 1; }

mkdir -p data
exec "$PY" -m telesurvey f2f-report "$@"
