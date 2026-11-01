#!/usr/bin/env bash
# =============================================================================
# export_combined.sh - one table of telesurvey and face-to-face answers.
#
# Telesurvey: submissions with call status 1 (picked up). Face-to-face: all.
# Columns: SN#, demographics, Question 1-4, source (TS / FF), idnumber.
# Writes data/exports/combined_<date>.xlsx and .csv. Changes nothing in KoBo.
#
#   ./export_combined.sh            # answers as codes, as stored in KoBo
#   ./export_combined.sh --labels   # answers as choice labels
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"

[[ -x "$PY" ]] || { echo "Not set up yet: run ./setup.sh first" >&2; exit 1; }
[[ -f config.json ]] || { echo "config.json missing: run ./setup.sh first" >&2; exit 1; }

exec "$PY" -m telesurvey export-combined "$@"
