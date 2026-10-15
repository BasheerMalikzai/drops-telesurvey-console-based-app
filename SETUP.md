# Setup guide

This guide takes you from nothing to a fully automatic daily telesurvey
assignment with an email report after every run. Plan for about an hour the
first time.

**Contents**

1. [How it works](#1-how-it-works)
2. [Choose where to run it](#2-choose-where-to-run-it)
3. [Prepare KoBoToolbox](#3-prepare-kobotoolbox)
4. [Prepare PostgreSQL](#4-prepare-postgresql)
5. [Prepare email](#5-prepare-email)
6. [Install and run setup](#6-install-and-run-setup)
7. [Map your form: callers and call results](#7-map-your-form-callers-and-call-results)
8. [Add surveyors and phone numbers](#8-add-surveyors-and-phone-numbers)
9. [Test run](#9-test-run)
10. [Schedule the runs](#10-schedule-the-runs)
11. [Settings reference](#11-settings-reference)
12. [Troubleshooting setup](#12-troubleshooting-setup)

---

## 1. How it works

```
 every morning (daily.sh)                          during the day (sync.sh)
 ─────────────────────────                         ────────────────────────
 1. download new submissions since the last run    every 20–360 minutes, in working
    (all of them, whatever their validation status)   hours (your choice):
 2. record each call result in call_list:            1. download new submissions
      successful / declined / invalid → done for good 2. apply their call results
      didn't pick up / busy           → try again    3. re-upload the callers' list,
        (until max_attempts)                            only if it changed
      rescheduled                     → on the date  no draw, no report email
 3. draw today's numbers from call_list; if there aren't enough,
    pull new random numbers from phone_pool into call_list
 4. rebuild assignments (today only) and upload assignments.csv to KoBo
 5. email the report (per caller: submitted, successful, rescheduled,
    declined, didn't pick up …) or the error, if the run failed
```

Everything is an **outgoing** connection (to KoBo, the mail server and the
database), so the computer running it needs no public address or web server.

**What lives where**

| Item | Location |
|---|---|
| Surveyors (caller IDs), rules, form field names, report recipients | `config.json` |
| KoBo token and form ID, database password, mail server login | `.env` (private) |
| Phone numbers to import | `data/phone_numbers.csv` |
| Master list (`phone_pool`), numbers being worked (`call_list`), today's draw (`assignments`), every submission | PostgreSQL database |
| Daily assignment files and logs | `data/` |

---

## 2. Choose where to run it

| Option | When to choose it | Notes |
|---|---|---|
| **Your Mac** | A pilot or a small team; no server to look after | Must be on and logged in during working hours (it may sleep: see section 10.2) |
| **Linux server (VPS)** | Always on, for larger or business-critical use | Hetzner, DigitalOcean, Linode, Vultr… |
| **Linux PC** | Like a Mac | Cron skips runs while the PC is asleep, so keep it awake |
| **PythonAnywhere (paid plan)** | You prefer a web dashboard over a command line | Tasks run at most hourly; PostgreSQL is a paid add-on |
| **Windows** | Possible through WSL (Windows Subsystem for Linux) | Start the scripts from Windows Task Scheduler |

You need Python 3.8 or newer, a PostgreSQL database, an email account that can
send through SMTP, and a KoBoToolbox account that owns the survey form.

---

## 3. Prepare KoBoToolbox

### 3.1 Get your KoBo details

| What | Where to find it |
|---|---|
| **API token** | KoBo → your account (top right) → Account Settings → Security → API Key |
| **Form UID** | Open the form in KoBo; it is the code after `/forms/` in the address bar, e.g. `aBcD3fGh1JkLmNoP` |
| **Server** | `https://kf.kobotoolbox.org` (global) or `https://eu.kobotoolbox.org` (EU) |

### 3.2 What the form needs

The tool is set up for your form's fields. Only these matter to it; every other
question (demographics, main questions, audio …) is left alone.

| Field in your form | Used for | Requirement |
|---|---|---|
| `idnumber` | Which caller made the call | Must hold the caller's ID exactly as in `config.json` (e.g. `101`) |
| `phnumber` | The number called | Type `select_one_from_file assignments.csv`, with choice_filter `surveyor_id=${idnumber}` |
| `recording_group/status_call` | Call result | A `select_one`; its choice **names** are mapped in section 7 |
| `reschedual` | Callback date | A date question shown when the call is rescheduled |
| `_validation_status` | Supervisor's review | Ignored by default; see section 3.3 |
| `_submission_time` | When the form was sent | Automatic; used for the report date |
| `_submitted_by` | Backup caller ID | Automatic; used when `idnumber` is empty |

The file `assignments.csv` that the tool uploads has these columns, which the
form can use:

| Column | Content | Example use in the form |
|---|---|---|
| `name` | Phone number (digits only) | The value stored in `phnumber` |
| `label` | Number and respondent name | What the caller sees in the list |
| `surveyor_id` | Caller the number is assigned to | `choice_filter: surveyor_id=${idnumber}` |
| `contact_name` | Respondent name | `pulldata('assignments', 'contact_name', 'name', ${phnumber})` for `auto_resp_name` |
| `attempt_no`, `previous_outcome` | Call history | Optional hints for the caller |
| `language` | Respondent's language | Show it to the caller, e.g. `pulldata('assignments', 'language', 'name', ${phnumber})` |
| `assigned_to`, `resp_phone_number`, `resp_name` | Same as `surveyor_id`, `name`, `contact_name`, under the phone list's names | For forms using the older `search()` appearance, e.g. `minimal search('Assignment', 'matches', 'assigned_to', ${idnumber})` |

The file name (`assignments.csv` by default) is set with `KOBO_MEDIA_FILENAME`
in `.env`, e.g. `KOBO_MEDIA_FILENAME="Assignment.csv"`. With `search()`, use
`matches` rather than `contains` for the caller ID (otherwise ID 232 also sees
2323's numbers); note that `search()` works in KoboCollect only, not in web forms.

Deploy the form. You don't need to upload `assignments.csv` by hand: the first
run uploads it and redeploys the form. The tool's names are configurable
(`kobo_fields` in `config.json`), so a form with different names works too.

### 3.3 Validation status (optional)

By default **every submission counts**, whatever its validation status in KoBo
(approved, not approved, on hold or not reviewed).

If you later want only **Approved** submissions to count, set
`kobo_import.required_validation_status` to `"validation_status_approved"` in
`config.json`. See OPERATIONS.md, "Requiring approval (optional)", for what
changes.

---

## 4. Prepare PostgreSQL

Setup creates the **tables** for you, but the **database and user** must exist
first. Run these once as a database administrator (choose your own password):

```sql
CREATE USER telesurvey WITH PASSWORD 'choose-a-strong-password';
CREATE DATABASE telesurvey OWNER telesurvey;
```

| Where | How |
|---|---|
| **Mac** | Install [Postgres.app](https://postgresapp.com) (simplest) or `brew install postgresql@16 && brew services start postgresql@16`, then open `psql` and run the SQL above. Host `localhost`, port `5432` |
| **VPS / Linux** | `sudo apt update && sudo apt install -y postgresql`, then `sudo -u postgres psql` and the SQL above |
| **PythonAnywhere** | Enable Postgres on the **Databases** tab (paid add-on); it shows the address, port and an admin user `super`. Run the SQL above in its Postgres console, then use the `telesurvey` user in setup |
| **Managed** (Neon, Supabase, DigitalOcean…) | Create the database and user in their dashboard; use SSL mode `require`. Good choice with a Mac: the data stays safe if the laptop is lost or its disk fails |

---

## 5. Prepare email

Reports are sent through an ordinary email account (SMTP). Have ready:

| Provider | Server | Port / security | Password |
|---|---|---|---|
| Gmail / Google Workspace | `smtp.gmail.com` | 587 / starttls | An **App Password** (Google Account → Security → 2-Step Verification → App passwords), not your normal password |
| Microsoft 365 / Outlook | `smtp.office365.com` | 587 / starttls | The account password (SMTP sending may need to be enabled by your admin) |
| Your organisation's server | ask IT | usually 587 / starttls or 465 / ssl | as given |

Also decide **who receives the report**: any number of addresses, editable at
any time (section 11).

---

## 6. Install and run setup

### 6.1 Copy the project to the computer

- **Mac:** move `telesurvey.zip` to your home folder (not Documents, Desktop or
  Downloads), then in **Terminal**: `cd ~ && unzip telesurvey.zip && cd telesurvey`
- **VPS / Linux:** copy `telesurvey.zip` over (e.g. `scp telesurvey.zip you@server:`), then
  `unzip telesurvey.zip && cd telesurvey`
- **PythonAnywhere:** upload the zip on the **Files** tab, then in a **Bash
  console**: `cd ~ && unzip telesurvey.zip && cd telesurvey`

```bash
chmod +x setup.sh daily.sh sync.sh verify_kobo_media.py
```

### 6.2 Run setup

| Where | Command |
|---|---|
| Mac, VPS or Linux | `./setup.sh` |
| PythonAnywhere | `./setup.sh --pythonanywhere` |

Setup installs what it needs, creates `config.json` and `.env`, and asks:

| Question | What to enter |
|---|---|
| KoBo API token / form UID | From section 3.1 (token typing is hidden) |
| Database engine | `postgres` |
| Host, port, database, user, password | From section 4 (password typing is hidden) |
| SSL mode | `prefer` on the same server, `require` for cloud databases |
| Schema | Press Enter, unless the database is shared with other apps |
| Send an email report after every run? | `Y` |
| Recipient addresses | e.g. `manager@org.org, supervisor@org.org` |
| SMTP server, security, port, user, password, sender | From section 5 |
| Time zone *(PythonAnywhere)* | e.g. `Africa/Nairobi`, so dates follow your local day |
| Morning time, days, sync interval and hours *(with `--schedule` or `--pythonanywhere`)* | See section 10 |

Setup then connects to the database, creates the tables, **sends a test
email** and tests the KoBo connection. If the database connection fails, it
shows the reason and lets you re-enter the details.

Setup is safe to run again; it never overwrites `config.json`, `.env` or your data.

| To change later | Run |
|---|---|
| Database details | `./setup.sh --database` |
| Mail server and recipients | `./setup.sh --email` |
| Only the recipients | `.venv/bin/python -m telesurvey set-recipients a@org.org b@org.org` |
| Schedule | Section 10.4 |
| KoBo token / form | edit `.env`, then `.venv/bin/python -m telesurvey check` |

### 6.3 Callers already used the form before this setup?

The first run only reads the last 14 days. To bring in older submissions once:

```bash
./daily.sh --since 2026-01-01
```

### 6.4 Upgrading from an earlier version

Existing databases are upgraded automatically the first time the new version
connects (new columns are added; existing data is kept). Data in an old SQLite
file can be copied with `.venv/bin/python -m telesurvey import-sqlite data/telesurvey.db`.

---

## 7. Map your form: callers and call results

The tool must know which `status_call` codes mean what. Once callers have sent
a few test forms, run:

```bash
.venv/bin/python -m telesurvey inspect
```

It reads the real submissions and shows every call-result code, caller ID and
validation status, and whether each is mapped:

```
  Call-result codes  (field 'recording_group/status_call')
         8  completed      -> successful (done)
         2  no_answer      -> no_pickup (retry)
         1  switched_off   -> NOT MAPPED: add it to 'outcomes' in config.json
  Callers  (field 'idnumber', falling back to '_submitted_by')
         5  101            in config.json
         1  esi.kobo       NOT in config.json surveyors
```

Put each code in the right category under `outcomes` in `config.json`. Use the
choice **name** from the form's choices sheet, not its label:

```json
"outcomes": {
  "successful":  { "action": "done",     "codes": ["completed"] },
  "rescheduled": { "action": "callback", "codes": ["rescheduled"] },
  "declined":    { "action": "done",     "codes": ["refused"] },
  "no_pickup":   { "action": "retry",    "codes": ["no_answer", "switched_off"] },
  "busy":        { "action": "retry",    "codes": ["busy"] },
  "invalid":     { "action": "done",     "codes": ["invalid_number"] }
}
```

| `action` | What happens to the number |
|---|---|
| `done` | Finished for good |
| `retry` | Called again on a later day, until `max_attempts` is reached |
| `callback` | Called again on the `reschedual` date, by the same caller |

The categories are also the **columns of the email report**. Successful,
Rescheduled, Declined and Didn't pick up are always shown; others appear when
used. You may rename, add or remove categories (add `"label": "Text"` to change
a column title). Unmapped codes are treated as *retry*, counted as *Other* in
the report, and logged as a warning.

Run `inspect` again until it says **Everything is mapped**.

---

## 8. Add surveyors and phone numbers

### 8.1 Surveyors: `config.json`

```json
"surveyors": [
  { "id": "101", "name": "Ama",  "phone": "+93700000001", "active": true, "languages": ["dari"] },
  { "id": "102", "name": "Kofi", "phone": "+93700000002", "active": true, "languages": ["pashto"] },
  { "id": "103", "name": "Esi",  "phone": "+93700000003", "active": true, "languages": ["dari", "pashto"],
    "daily_quota": 20 }
]
```

| Field | Meaning |
|---|---|
| `id` | Caller ID: **the value callers enter in `idnumber`** |
| `name`, `phone` | Shown in the report |
| `active` | `false` = gets no numbers (on leave, left the team) |
| `daily_quota` | Optional: numbers per day for this person |
| `languages` | Languages this caller speaks, e.g. `["dari"]`, `["pashto"]` or both. Callers only get numbers in their languages. Leave it out and the caller can take any language |
| `aliases` | Optional: other ways the phone list's `assigned_to` column names this caller, e.g. `["Ama Boateng", "12"]` |

### 8.2 Phone numbers: `data/phone_numbers.csv`

```csv
phone,name,language
+93 70 010 0001,Abena Mensah,Dari
0700100002,Yaw Owusu,Pashto
```

Only `phone` is required. Spaces, dashes and `+` are removed (numbers are stored
as digits); rows with fewer than 7 digits are skipped. Add rows at any time:
known numbers are ignored. Columns can also be named `resp_phone_number`,
`Phone Number`, `mobile`… and `resp_name`…; the file may use commas, semicolons
or tabs. Other columns (province, weight…) are ignored.

**`language`** is the respondent's language. `Dari`, `dari`, `Persian`, `Farsi`,
`prs` and `دری` all count as Dari; `Pashto`, `Pushto`, `ps` and `پښتو` as Pashto.
Another column name can be set in `config.json` under `phone_list.language_columns`.

| Situation | What happens |
|---|---|
| Number has a language | Only callers who speak it get it (retries and callbacks too) |
| Number has no language | Any caller may get it; bilingual callers first |
| Language nobody active speaks (e.g. Uzbek) | Not assigned; the log and report warn how many are waiting |
| Language column added to the file later | The next run fills in the language for numbers already imported |

**`assigned_to`** (optional) reserves a number for one caller: put the caller's
`id` (or `name`, or one of their `aliases`) from `config.json`.

| Situation | What happens |
|---|---|
| `assigned_to` names an active caller | Only that caller ever gets the number (language isn't checked). Their own numbers come before random ones, after callbacks due |
| More reserved numbers than the caller's quota | The rest wait for the next days |
| Empty | Drawn at random as usual |
| Names someone not active in `config.json` | Treated as empty; the log warns how many |
| Changed in the file later | The next run follows the new value |

---

## 9. Test run

```bash
.venv/bin/python -m telesurvey check        # database, KoBo, form question names
.venv/bin/python -m telesurvey test-email   # mail settings
./daily.sh --no-upload                      # full run without sending anything to KoBo
```

Look at `data/assignments/assignments_<date>.csv` and the report in your inbox,
then do a real run and check that the file reached the phones:

```bash
./daily.sh --force                          # draw again, upload, email the report
.venv/bin/python verify_kobo_media.py       # is assignments.csv on KoBo and served to phones?
```

Finally open the form in KoboCollect, refresh it, enter a caller ID and check
the numbers appear.

---

## 10. Schedule the runs

Two jobs run automatically:

| Job | When | What it does |
|---|---|---|
| **Morning run** (`daily.sh`) | Once a day, e.g. 06:00 | Applies new results, draws today's numbers, uploads them, emails the report |
| **Sync** (`sync.sh`) | Every 20–360 minutes during working hours, e.g. every 30 minutes from 07:00 to 19:00 | Downloads new submissions, applies the results, re-uploads the callers' list **only if it changed**. No draw, no report email |

The sync emails only when something goes wrong: **one** email when it starts
failing, and **one** when it works again, never one per failed attempt.

### 10.1 Mac or Linux

```bash
./setup.sh --schedule
```

It asks for the morning run time, the days, the sync interval (20–360 minutes,
or 0 = no syncing during the day) and the sync hours, then installs both jobs:
**launchd** on a Mac, **cron** on Linux. Check them at any time:

```bash
.venv/bin/python -m telesurvey schedule
```

Times are the computer's own time zone. On a Linux server, set it once with
`sudo timedatectl set-timezone Africa/Nairobi`.

### 10.2 Running it on a Mac

The Mac acts as the server, so a few things matter:

| Situation | What happens |
|---|---|
| **Asleep** at the morning time | launchd runs the morning job as soon as the Mac wakes |
| **Switched off** at the morning time | The first sync check after you start the Mac and log in does the morning run instead |
| Asleep or off during the day | Syncs simply resume when the Mac wakes; the first one catches up on everything |
| Both jobs start together when the Mac wakes | Only one morning run happens and one report is sent |

The jobs run for your user account, so **you must be logged in**. A locked
screen is fine; the login screen after a restart is not, as nothing runs until
someone logs in.

To have the list ready **before** callers start, let the Mac wake itself shortly
before the morning run:

```bash
./setup.sh --wake          # asks for your Mac password once
```

This sets a daily wake 5 minutes before the morning run (`pmset repeat`; it
replaces any other repeating wake time on the Mac). Keep the Mac **plugged in**
for this to be reliable; with the lid closed on battery it may stay asleep, and
the run then happens when the Mac is opened. While a job runs, the Mac is kept
awake (`caffeinate`), so it can't fall asleep halfway through.

Keep the project **outside** Documents, Desktop, Downloads and iCloud Drive
(e.g. in `~/telesurvey`): macOS blocks scheduled jobs from those folders, and
`--schedule` refuses to install there.

### 10.3 PythonAnywhere

`./setup.sh --pythonanywhere` prints the exact commands for two tasks on the
**Tasks** tab:

1. A **daily** task running `/home/YOURNAME/telesurvey/daily.sh --scheduled`. **The time is
   UTC** (06:00 in Nairobi = 03:00 UTC).
2. An **hourly** task running `/home/YOURNAME/telesurvey/sync.sh`. PythonAnywhere
   runs tasks at most hourly, so a sync interval under 60 minutes behaves like 60.

### 10.4 Changing the schedule later

| Change | Command | Reinstall needed? |
|---|---|---|
| Sync interval | `.venv/bin/python -m telesurvey set-refresh 45` | No |
| Sync hours | `.venv/bin/python -m telesurvey set-schedule --from 08:00 --until 18:00` | No |
| Morning time or days | `.venv/bin/python -m telesurvey set-schedule --morning 05:30 --days mon,tue,wed,thu,fri,sat` | Done automatically |
| Stop everything | `./setup.sh --unschedule` | |

---

## 11. Settings reference

### `config.json`

| Key | Default | Meaning |
|---|---|---|
| `surveyors` | | Section 8.1 |
| `assignment.numbers_per_surveyor` | 30 | Numbers per caller per day |
| `assignment.max_attempts` | 5 | A number with this many calls and no final result becomes `exhausted` and isn't called again. `0` = no limit |
| `assignment.reschedules_count_as_attempts` | false | `false`: "call me later" doesn't use up an attempt. `true`: every call counts |
| `pull.batch_size` | 0 | `0`: pull just enough new numbers from `phone_pool` to fill each day. e.g. `500`: whenever the call list runs short, pull 500 at once and work through them first |
| `assignment.sticky_reschedule` | true | Rescheduled calls go back to the caller who booked them |
| `outcomes` | | Call-result categories and codes (section 7) |
| `kobo_fields.phone` | `phnumber` | Number question |
| `kobo_fields.call_status` | `recording_group/status_call` | Call-result question (group path allowed) |
| `kobo_fields.surveyor` | `idnumber` | Caller ID question |
| `kobo_fields.surveyor_fallback` | `_submitted_by` | Used when the caller ID is empty |
| `kobo_fields.callback_date` | `reschedual` | Callback date question |
| `kobo_fields.validation_status` / `submission_time` | `_validation_status` / `_submission_time` | KoBo's own fields |
| `kobo_import.required_validation_status` | `""` | `""` = every submission counts, whatever its validation status. `"validation_status_approved"` = only approved ones count |
| `kobo_import.lookback_days` | 14 | How far back the **first** download reaches. (When approval is required, every run reads at least this far back to catch late approvals) |
| `kobo_import.overlap_hours` | 24 | Each download starts this long before the previous successful one, as a safety margin; already-seen submissions are skipped |
| `report.enabled` | true | Send email reports |
| `report.recipients` | | List of addresses |
| `report.send_on` | `always` | `always`, `failure` (only when something went wrong) or `never` |
| `report.report_day` | `yesterday` | Which day's calls the report covers (`yesterday` or `today`) |
| `report.subject_prefix` | `[Telesurvey]` | Start of every subject line, handy for email filters |
| `schedule.morning_run` | `06:00` | Time of the morning run |
| `schedule.days` | every day | Days for the morning run and syncs, e.g. `["mon", "tue", "wed", "thu", "fri", "sat"]` |
| `schedule.sync_every_minutes` | 30 | How often the sync runs: 20–360, or `0` = no syncing during the day |
| `schedule.sync_from` / `sync_until` | `07:00` / `19:00` | Syncs only run between these times |
| `schedule.sync_overlap_minutes` | 60 | Each sync starts this long before the previous download, as a safety margin |
| `paths.*` | under `data/` | Phone list, SQLite file, output folder, log file |

### `.env`

| Key | Meaning |
|---|---|
| `KOBO_TOKEN`, `KOBO_ASSET_UID`, `KOBO_URL` | KoBo connection |
| `KOBO_MEDIA_FILENAME` | Must match `select_one_from_file` (default `assignments.csv`) |
| `KOBO_REDEPLOY` | `1` = redeploy after each upload so phones get the new list |
| `KOBO_USERNAME`, `KOBO_PASSWORD` | Optional, only for `verify`'s phone check |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` | Mail server (`starttls`, `ssl` or `none`) |
| `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Mail login and sender address |
| `DB_ENGINE` | `postgres` (or `sqlite` for quick local tests) |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`, `PG_SCHEMA` | PostgreSQL |
| `DATABASE_URL` | Optional single URL instead of the `PG*` settings |
| `TZ` | Your time zone on UTC servers such as PythonAnywhere, e.g. `Africa/Nairobi`. Not needed on a Mac |

Keep `.env` private: it holds passwords. Setup makes it readable only by you.

---

## 12. Troubleshooting setup

| Problem | Fix |
|---|---|
| `python3: command not found` / too old | Install Python 3.8+ (Ubuntu: `sudo apt install python3 python3-venv`; Mac: `xcode-select --install`) |
| `Could not create .venv` | Ubuntu/Debian: `sudo apt install python3-venv` |
| `password authentication failed` | `./setup.sh --database` |
| `Is the server running …?` | Wrong host/port, database stopped, or a firewall |
| `database "telesurvey" does not exist` | Run the SQL in section 4 |
| Test email: `Authentication failed` / `Username and Password not accepted` | Gmail needs an App Password; check `SMTP_USER`/`SMTP_PASSWORD` with `./setup.sh --email` |
| Test email: `Connection refused` / timed out | Wrong server or port, or outgoing mail blocked by the host's firewall |
| Test email sent but nothing arrives | Check spam; set `SMTP_FROM` to an address the mail account may send as |
| `inspect` shows `NOT MAPPED` codes | Add them to `outcomes` (section 7) |
| `inspect` shows callers `NOT in config.json` | Add them to `surveyors`, or have callers enter their correct `idnumber` |
| `check` says a question name is missing | Rename it in the form, or change `kobo_fields` |
| No results applied although calls were made | Check `inspect`; if `required_validation_status` is set, submissions must be **Approved** in KoBo |
| Phones don't show today's numbers | `.venv/bin/python verify_kobo_media.py` |
| `--schedule` says the project is inside Documents/Desktop/Downloads | Move the folder to `~/telesurvey` and run `./setup.sh --schedule` again |
| Mac: nothing ran this morning | Was the Mac logged in? Check `.venv/bin/python -m telesurvey schedule` and `data/launchd-daily.log` |
| Mac: `--wake` didn't wake it | Keep it plugged in; check the wake time with `pmset -g sched` |
| Linux: `crontab is not installed` | `sudo apt install cron` |

Day-to-day running is covered in [OPERATIONS.md](OPERATIONS.md).
