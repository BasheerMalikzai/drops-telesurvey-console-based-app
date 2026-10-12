#!/usr/bin/env bash
# =============================================================================
# setup.sh - one-time setup for the telesurvey assignment tool.
#
#   ./setup.sh                          # install, create config, test KoBo and email
#   ./setup.sh --schedule               # also schedule the morning run and the sync job
#                                       #   (launchd on a Mac, cron on Linux)
#   ./setup.sh --wake                   # Mac: wake the Mac 5 minutes before the morning run
#   ./setup.sh --unschedule             # remove the schedule
#   ./setup.sh --refresh-minutes 30     # how often the sync job runs (20-360, 0 = off)
#   ./setup.sh --database               # (re)enter the PostgreSQL connection details
#   ./setup.sh --email                  # (re)enter report recipients and mail server
#   ./setup.sh --pythonanywhere         # settings + step-by-step guide for PythonAnywhere
#   (options can be combined)
#
# Safe to run again: existing config.json, .env and data are never overwritten.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
PY="$PROJECT_DIR/.venv/bin/python"

say()  { printf '\n== %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SCHEDULE=0; UNSCHEDULE=0; WAKE=0; PYTHONANYWHERE=0; ASK_DB=0; REFRESH_MINUTES=""; ASK_EMAIL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --schedule|--install-cron)
      SCHEDULE=1
      # older versions took a cron expression here; times now come from config.json
      if [[ -n "${2:-}" && "${2:0:2}" != "--" ]]; then
        echo "Note: ignoring '$2'; set the times with: .venv/bin/python -m telesurvey set-schedule"
        shift
      fi ;;
    --unschedule) UNSCHEDULE=1 ;;
    --wake) WAKE=1 ;;
    --pythonanywhere) PYTHONANYWHERE=1 ;;
    --database) ASK_DB=1 ;;
    --email) ASK_EMAIL=1 ;;
    --refresh-minutes)
      [[ -n "${2:-}" ]] || fail "--refresh-minutes needs a number (20-360, or 0 for off)"
      REFRESH_MINUTES="$2"; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) fail "Unknown option: $1 (see ./setup.sh --help)" ;;
  esac
  shift
done

# Write KEY="value" into .env, replacing an existing line for KEY.
# Values are wrapped in quotes so passwords may contain spaces, #, $ and quotes.
set_env() {
  python3 - "$1" "$2" <<'PYEOF'
import sys, pathlib
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = p.read_text().splitlines() if p.exists() else []
new = f'{key}="{value}"'
out, done = [], False
for line in lines:
    if line.split("=", 1)[0].strip() == key:
        out.append(new); done = True
    else:
        out.append(line)
if not done:
    out.append(new)
p.write_text("\n".join(out) + "\n")
PYEOF
}

# Load .env into shell variables safely (never with `source`, which would
# interpret $, spaces and quotes inside passwords). Blank values are unset.
load_env() {
  [[ -f .env ]] || return 0
  eval "$(python3 - <<'PYEOF'
import pathlib, shlex
for line in pathlib.Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if key.startswith("export "):
        key = key[7:].strip()
    if not key.isidentifier():
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    print(f"export {key}={shlex.quote(value)}" if value else f"unset {key}")
PYEOF
)"
}

# ask KEY "Question" "default"
ask() {
  local key="$1" prompt="$2" def="$3" ans
  read -rp "$prompt [${def}]: " ans
  set_env "$key" "${ans:-$def}"
}

configure_db() {
  say "Database settings"
  load_env
  local engine pw
  read -rp "Database engine, postgres or sqlite [${DB_ENGINE:-postgres}]: " engine
  engine="${engine:-${DB_ENGINE:-postgres}}"
  case "$engine" in postgres|sqlite) ;; *) fail "Choose postgres or sqlite" ;; esac
  set_env DB_ENGINE "$engine"
  if [[ "$engine" == postgres ]]; then
    if [[ $PYTHONANYWHERE == 1 ]]; then
      echo "PythonAnywhere: the address, port and 'super' user are on the Databases tab > Postgres."
    fi
    ask PGHOST     "Host"                          "${PGHOST:-localhost}"
    ask PGPORT     "Port"                          "${PGPORT:-5432}"
    ask PGDATABASE "Database name"                 "${PGDATABASE:-telesurvey}"
    ask PGUSER     "User"                          "${PGUSER:-}"
    read -rsp "Password$([[ -n "${PGPASSWORD:-}" ]] && echo ' (Enter keeps the current one)'): " pw; echo
    [[ -n "$pw" ]] && set_env PGPASSWORD "$pw"
    ask PGSSLMODE  "SSL mode: disable, prefer or require" "${PGSSLMODE:-prefer}"
    ask PG_SCHEMA  "Schema (Enter = default 'public')"    "${PG_SCHEMA:-}"
  fi
  load_env
}

# Ask for the schedule (morning run time, sync interval and hours) and save it in config.json
ask_schedule() {
  local ans out
  read -r CUR_MORNING CUR_EVERY CUR_FROM CUR_UNTIL CUR_DAYS < <("$PY" -c \
    'from telesurvey.config import load_config as l; c = l(); print(c.morning_time, c.sync_every_minutes, c.sync_from, c.sync_until, ",".join(c.run_days))')
  echo "The morning run draws the day's numbers, uploads them and emails the report."
  while true; do
    read -rp "Morning run time (HH:MM) [${CUR_MORNING}]: " ans
    out="$("$PY" -m telesurvey set-schedule --morning "${ans:-$CUR_MORNING}")" && break; echo "$out"
  done
  while true; do
    read -rp "Run on which days (e.g. mon,tue,wed,thu,fri,sat) [${CUR_DAYS}]: " ans
    out="$("$PY" -m telesurvey set-schedule --days "${ans:-$CUR_DAYS}")" && break; echo "$out"
  done
  echo "During the day, the sync job downloads new results and refreshes the callers' list."
  while true; do
    read -rp "Sync every how many minutes (20-360, 0 = off) [${CUR_EVERY}]: " ans
    out="$("$PY" -m telesurvey set-refresh "${ans:-$CUR_EVERY}")" && break; echo "$out"
  done
  while true; do
    read -rp "First sync of the day (HH:MM) [${CUR_FROM}]: " ans; local f="${ans:-$CUR_FROM}"
    read -rp "Last sync of the day (HH:MM) [${CUR_UNTIL}]: " ans; local u="${ans:-$CUR_UNTIL}"
    "$PY" -m telesurvey set-schedule --from "$f" --until "$u" && break
  done
}

configure_email() {
  say "Email reports"
  load_env
  local ans current def
  read -rp "Send an email report after every run? [Y/n]: " ans
  if [[ "$ans" =~ ^[Nn] ]]; then
    "$PY" -m telesurvey set-recipients
    return 0
  fi
  current="$("$PY" -c 'from telesurvey.config import load_config as l; print(", ".join(r for r in l().report_recipients if not r.endswith("@example.org")))' 2>/dev/null || true)"
  while true; do
    read -rp "Recipient addresses, separated by commas [${current}]: " ans
    ans="${ans:-$current}"
    [[ -n "$ans" ]] && "$PY" -m telesurvey set-recipients "$ans" && break
  done
  echo "Mail server for sending (Gmail: smtp.gmail.com with an App Password; Microsoft 365: smtp.office365.com)"
  ask SMTP_HOST     "SMTP server"                         "${SMTP_HOST:-}"
  ask SMTP_SECURITY "Security: starttls, ssl or none"     "${SMTP_SECURITY:-starttls}"
  load_env
  case "${SMTP_SECURITY:-starttls}" in ssl) def=465 ;; none) def=25 ;; *) def=587 ;; esac
  ask SMTP_PORT     "Port"                                "${SMTP_PORT:-$def}"
  ask SMTP_USER     "Login user name (usually your email address)" "${SMTP_USER:-}"
  read -rsp "Password$([[ -n "${SMTP_PASSWORD:-}" ]] && echo ' (Enter keeps the current one)'): " ans; echo
  [[ -n "$ans" ]] && set_env SMTP_PASSWORD "$ans"
  load_env
  ask SMTP_FROM     "Sender address shown in the email"   "${SMTP_FROM:-${SMTP_USER:-}}"
  load_env
  echo "Sending a test email..."
  "$PY" -m telesurvey test-email || echo "Fix the mail settings with ./setup.sh --email (or edit SMTP_* in .env)"
}

db_help() {
  cat <<HELP
  Check that the host and port are right, the server is running and reachable from here,
  and the user name and password are correct. Cloud databases often need SSL mode 'require'.
  If the database or user doesn't exist yet, an administrator can create them with:
      CREATE USER ${PGUSER:-telesurvey} WITH PASSWORD 'choose-a-password';
      CREATE DATABASE ${PGDATABASE:-telesurvey} OWNER ${PGUSER:-telesurvey};
  Then re-enter the settings with:  ./setup.sh --database
HELP
}

say "Checking Python"
command -v python3 >/dev/null || fail "python3 is not installed"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || fail "Python 3.8 or newer is required (found $(python3 --version))"
echo "$(python3 --version) found"

say "Creating virtual environment and installing packages"
if [[ ! -x "$PY" ]]; then
  python3 -m venv .venv || fail "Could not create .venv. On Ubuntu/Debian: sudo apt install python3-venv"
fi
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt
echo "Installed: $("$PY" -m pip freeze | grep -iE '^(requests|psycopg)=' | tr '\n' ' ')"

say "Creating configuration files"
if [[ -f config.json ]]; then
  echo "config.json already exists, keeping it"
else
  cp config.example.json config.json
  echo "Created config.json: edit it to list your surveyors (caller IDs)"
fi
if [[ -f .env ]]; then
  echo ".env already exists, keeping it"
else
  cp .env.example .env
  echo "Created .env"
fi
chmod 600 .env

# Ask for the KoBo details if they're missing and someone is at the keyboard
load_env
if [[ -t 0 ]]; then
  if [[ -z "${KOBO_TOKEN:-}" ]]; then
    read -rsp "KoBo API token (Enter to skip): " answer; echo
    [[ -n "$answer" ]] && set_env KOBO_TOKEN "$answer"
  fi
  if [[ -z "${KOBO_ASSET_UID:-}" ]]; then
    read -rp "KoBo form UID (Enter to skip): " answer
    [[ -n "$answer" ]] && set_env KOBO_ASSET_UID "$answer"
  fi
  load_env
fi

load_env
if [[ -t 0 ]]; then
  if [[ $ASK_DB == 1 ]] || [[ "${DB_ENGINE:-postgres}" == postgres && -z "${PGUSER:-}" && -z "${DATABASE_URL:-}" ]]; then
    configure_db
  fi
fi

say "Connecting to the database and creating tables"
tries=0
until "$PY" -m telesurvey init; do
  tries=$((tries + 1))
  db_help
  if [[ -t 0 && $tries -lt 3 ]]; then
    read -rp "Re-enter the database settings now? [Y/n]: " answer
    [[ "$answer" =~ ^[Nn] ]] && break
    configure_db
  else
    echo "Fix the database settings in .env (or run ./setup.sh --database), then run setup again."
    exit 1
  fi
done

load_env
if [[ -t 0 ]] && [[ $ASK_EMAIL == 1 || -z "${SMTP_HOST:-}" ]]; then
  configure_email
fi

if [[ -n "$REFRESH_MINUTES" ]]; then
  say "Sync interval"
  "$PY" -m telesurvey set-refresh "$REFRESH_MINUTES" || fail "Invalid --refresh-minutes value"
fi

say "Testing the KoBo connection"
if ! "$PY" -m telesurvey check; then
  echo "(Fix the items above, then run: .venv/bin/python -m telesurvey check)"
fi

chmod +x daily.sh sync.sh verify_kobo_media.py

if [[ $PYTHONANYWHERE == 1 && ( $SCHEDULE == 1 || $WAKE == 1 ) ]]; then
  echo "Note: on PythonAnywhere the jobs are scheduled on the Tasks tab (steps below)."
  SCHEDULE=0; WAKE=0
fi

if [[ $UNSCHEDULE == 1 ]]; then
  say "Removing the schedule"
  "$PY" -m telesurvey schedule remove
fi

if [[ $SCHEDULE == 1 ]]; then
  say "Schedule"
  if [[ -t 0 ]]; then ask_schedule; fi
  "$PY" -m telesurvey schedule install || fail "Could not install the schedule (see the message above)"
  if [[ "$(uname)" == "Darwin" && $WAKE == 0 && -t 0 ]]; then
    read -rp "Wake the Mac automatically 5 minutes before the morning run? (needs your password) [Y/n]: " ans
    [[ "$ans" =~ ^[Nn] ]] || WAKE=1
  fi
fi

if [[ $WAKE == 1 ]]; then
  say "Waking the Mac before the morning run"
  if [[ "$(uname)" != "Darwin" ]]; then
    echo "This option is only for macOS."
  else
    WAKE_CMD="$("$PY" -c 'from telesurvey.config import load_config as l; from telesurvey.schedule import wake_command as w; print(w(l()))')"
    echo "Running: sudo $WAKE_CMD"
    echo "(This replaces any other repeating wake time set on this Mac.)"
    if sudo $WAKE_CMD; then
      pmset -g sched | sed 's/^/  /'
      echo "For a reliable wake keep the Mac plugged in. With the lid closed on battery it may"
      echo "stay asleep; the run then happens as soon as the Mac is opened."
    else
      echo "Could not set the wake time. Set it by hand with: sudo $WAKE_CMD"
    fi
  fi
fi

if [[ $PYTHONANYWHERE == 1 ]]; then
  say "PythonAnywhere settings"
  load_env
  set_env SQLITE_WAL 0   # only used if DB_ENGINE=sqlite (network storage)

  if [[ -z "${TZ:-}" && -t 0 ]]; then
    echo "PythonAnywhere runs on UTC. Your time zone keeps assignment dates on your local day."
    read -rp "Time zone (e.g. Africa/Accra, Africa/Nairobi, Asia/Kolkata; Enter = UTC): " answer
    [[ -n "$answer" ]] && set_env TZ "$answer"
  fi
  load_env
  if [[ -t 0 ]]; then ask_schedule; fi
  read -r MORNING EVERY < <("$PY" -c 'from telesurvey.config import load_config as l; c = l(); print(c.morning_time, c.sync_every_minutes)')

  cat <<GUIDE

PythonAnywhere: add two tasks on the Tasks tab (times there are UTC)
--------------------------------------------------------------------
  1. Scheduled task, daily, at the UTC equivalent of $MORNING:
       $PROJECT_DIR/daily.sh
  2. Scheduled task, hourly:
       $PROJECT_DIR/sync.sh
     PythonAnywhere runs tasks at most hourly, so a sync interval under 60 minutes
     (currently $EVERY) behaves like 60 there. The hourly task also does a missed
     morning run if task 1 didn't run.
GUIDE
fi

say "Setup finished"
cat <<EOF
Next steps:
  1. List your surveyors in config.json
  2. Put your numbers in data/phone_numbers.csv (columns: phone,name)
  3. Try a run without touching KoBo:  ./daily.sh --no-upload
EOF
if [[ $PYTHONANYWHERE == 0 ]]; then
  echo "  4. Schedule it (if you haven't):      ./setup.sh --schedule"
  echo "     Check the schedule any time:       .venv/bin/python -m telesurvey schedule"
else
  echo "  4. Add the two tasks printed above"
fi
