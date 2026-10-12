#!/usr/bin/env bash
# =============================================================================
# sync.sh - the sync job (started every 5 minutes by the schedule; see setup.sh --schedule).
#
# Most of the time it exits straight away. It only does work when:
#   - it is within sync hours and schedule.sync_every_minutes have passed since the last
#     sync: download new submissions, apply the results, re-upload the call list if it
#     changed (no draw, no report email), or
#   - the morning run should have happened today but hasn't (e.g. the Mac was switched
#     off at that time): then it does the full daily run instead.
#
#   ./sync.sh --now            # sync right away, whatever the time
#   ./sync.sh --no-catch-up    # never do a missed morning run
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"

[[ -x "$PY" ]] || { echo "Not set up yet: run ./setup.sh first" >&2; exit 1; }
[[ -f config.json ]] || { echo "config.json missing: run ./setup.sh first" >&2; exit 1; }
mkdir -p data
exec "$PY" -m telesurvey sync "$@"
