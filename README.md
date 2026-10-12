# Telesurvey daily assignment

Every morning this tool randomly assigns phone numbers to your telesurveyors
and uploads each person's list to a KoBoToolbox form. Numbers that were
answered or declined are removed; numbers that didn't pick up or asked for a
callback come back on a later day. Every submission counts, whatever its
validation status (requiring approval is optional).

During the day a sync job runs every 20–360 minutes: it applies new results and
refreshes the callers' list, so finished numbers disappear. After every morning
run an **email report** goes to the people you choose: per caller, how many
forms were submitted, successful, rescheduled, declined and not picked up, or
what went wrong if the run failed.

It only makes outgoing connections, so it runs on an ordinary Mac or Linux
computer: no web server or public address needed.

| Guide | For |
|---|---|
| **[SETUP.md](SETUP.md)** | Installing: KoBo form, PostgreSQL, email, setup script, scheduling (including on a Mac) |
| **[OPERATIONS.md](OPERATIONS.md)** | Running it day to day: monitoring, routine changes, fixing problems, backups |

## Quick start

    cd ~ && unzip telesurvey.zip && cd telesurvey
    ./setup.sh                     # or: ./setup.sh --pythonanywhere
    nano config.json               # your surveyors and report recipients
    .venv/bin/python -m telesurvey inspect   # map your form's call-result codes
    nano data/phone_numbers.csv    # columns: phone,name
    ./daily.sh --no-upload         # test run
    ./setup.sh --schedule          # morning run + sync (launchd on a Mac, cron on Linux)
    ./setup.sh --wake              # Mac: wake up before the morning run

## Project layout

| Path | Purpose |
|---|---|
| `setup.sh` | Install, configure, test, schedule; safe to re-run |
| `daily.sh` | The morning run |
| `sync.sh` | The sync job during the day |
| `verify_kobo_media.py` | Checks that `assignments.csv` reached KoBo and works as a form attachment |
| `config.json` | Surveyors, call-result categories, form field names, report recipients, schedule |
| `.env` | Private: KoBo token, PostgreSQL and mail-server credentials, time zone |
| `telesurvey/` | Python package (`config`, `db`, `phones`, `kobo`, `results`, `assign`, `export`, `report`, `sync`, `schedule`, `inspect_form`, `verify`) |
| `data/` | Phone list, assignment files, logs |
