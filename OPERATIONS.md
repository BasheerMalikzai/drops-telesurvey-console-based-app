# Daily operations guide

How to run, monitor and adjust the telesurvey system once it is set up
(see [SETUP.md](SETUP.md) for installation).

Commands are run from the project folder (`cd ~/telesurvey`). `ts` below is
short for `.venv/bin/python -m telesurvey`; type it in full, or create the
shortcut once with `alias ts='.venv/bin/python -m telesurvey'`.

**Contents**

1. [A normal day](#1-a-normal-day)
2. [Requiring approval (optional)](#2-requiring-approval-optional)
3. [The email report](#3-the-email-report)
4. [What happens to each number](#4-what-happens-to-each-number)
5. [Checking on things](#5-checking-on-things)
6. [Routine changes](#6-routine-changes)
7. [Fixing problems](#7-fixing-problems)
8. [Maintenance](#8-maintenance)
9. [Command reference](#9-command-reference)

---

## 1. A normal day

| When | What happens | Who |
|---|---|---|
| Morning (scheduled, e.g. 06:00) | `daily.sh` downloads every submission since the last run, applies their call results, draws today's numbers, uploads them to KoBo and **emails the report** | Automatic |
| Start of shift | Callers refresh the form in KoboCollect, enter their caller ID (`idnumber`) and start calling | Callers |
| Each call | Caller picks the number, records the call result, completes the survey if the person agrees, and sends | Callers |
| During the day (scheduled, e.g. every 30 min) | `sync.sh` downloads new submissions, applies them and, if the list changed, re-uploads it without the finished numbers | Automatic |
| End of shift | Callers send any forms still in their outbox | Callers |

**Tell callers:**

- Refresh the form at the start of the day, and after breaks, to get the shortened list.
- Enter **your own caller ID** in `idnumber`; it decides whose numbers you see and who gets credit in the report.
- Always record a **call result**, also when nobody answers: that is what brings the number back another day.
- When someone asks to be called back, choose *rescheduled* and fill in the date: the number returns **to you** on that day.
- Send forms the same day.

---

## 2. Requiring approval (optional)

**By default every submission counts**, whatever its validation status in KoBo.
A supervisor marking a submission "Not approved" does not change anything.

To make only approved submissions count, set in `config.json`:

```json
"kobo_import": { "required_validation_status": "validation_status_approved", "lookback_days": 14 }
```

With that setting:

| Validation status | Effect on the number |
|---|---|
| *(not reviewed yet)* or **On Hold** | **Held** (status `review`): not given to anyone, not counted yet |
| **Approved** | The call result is applied |
| **Not approved** | The call doesn't count: the number goes back into the pool |

- Reviews can happen days later: in this mode every run reads at least the last `lookback_days`.
- Unreviewed submissions older than `lookback_days` release their numbers.
- The report gains *Approved*, *Awaiting review* and *Rejected* columns.
- During the day the sync holds a called number (it leaves the list) but applies its result only once it is approved.

Set it back to `""` to count everything again. Numbers held at that moment are
resolved at the next run.

---

## 3. The email report

After **every** run an email goes to the addresses in `report.recipients`
(`config.json`).

**When the run succeeds:** subject `[Telesurvey] Daily run OK 2026-10-06: 90 numbers assigned`

| Section | Content |
|---|---|
| Calls submitted on *yesterday*, per caller | Submitted · **Successful · Rescheduled · Declined · Didn't pick up** · other categories in use (e.g. Busy, Other), with a total row |
| This run | New numbers imported, submissions checked, new results applied, new numbers pulled into the call list, numbers assigned today, upload result |
| Today's assignment | Numbers per caller |
| Numbers | Size of `phone_pool` and how many are not pulled yet; `call_list` per status and per last result |

- The day is taken from `_submission_time`, converted to your time zone
  (a form sent at 01:30 local time counts for that day, not the previous one).
- Every submission is counted, whatever its validation status. Ask for a fresh
  copy any time: `ts report` (yesterday) or `ts report --date 2026-10-05`.
  KoBo stamps `_submission_time` when the form **reaches the server**, so a form
  that waited on a phone counts for the day it was sent, not the day of the call.
- Callers are shown as `ID Name` from `config.json`. A row with a login name
  (e.g. `esi.kobo`) means `idnumber` was empty and `_submitted_by` was used.

**When something fails:** subject `[Telesurvey] FAILED: daily run …` or
`[Telesurvey] Upload FAILED: …`, with the step that failed, the error, what to
do, the last log lines and technical details. The per-caller table is included
whenever the database could be reached.

| Setting (`config.json` → `report`) | Options |
|---|---|
| `recipients` | any list of addresses (`ts set-recipients a@org.org b@org.org`) |
| `send_on` | `always` · `failure` (only problems) · `never` |
| `report_day` | `yesterday` (default) · `today` |
| `subject_prefix` | text at the start of every subject |
| `enabled` | `false` switches reports off |

A broken mail setup never stops the daily run: the run completes and the log
says `Could not send the email report: …`.

---

## 4. What happens to each number

```
 phone_pool ──pull──► call_list ──draw──► assignments
 (master list,        (numbers being        (today only:
  never changed)       worked: status,        number, caller,
                       last result, dates)    called yet?)
```

| Table | Holds | Changed by |
|---|---|---|
| `phone_pool` | Every number you imported: phone, name, date added | Only the import, which adds new numbers. Never changed otherwise |
| `call_list` | Numbers pulled for calling, with **status**, **last result** (successful, rescheduled, declined, didn't pick up …), **attempts**, **first and last contact date**, last caller, callback date | Pulls and call results |
| `assignments` | Today's draw only: number, caller, date, called yet | Rebuilt every morning |
| `submissions` | Every KoBo submission (the full call history) | Incoming results |

The history of earlier days' draws is kept in the files
`data/assignments/assignments_<date>.csv`.

**`call_list` status**

| Status | Meaning |
|---|---|
| `open` | Waiting to be called (new, retry, or callback) |
| `done` | Finished for good (successful, declined, invalid) |
| `exhausted` | Reached `max_attempts` calls without a final result; not called again |
| `removed` | Taken out of calling by hand (`ts remove`) |
| `review` | Only when approval is required: called, submission awaiting review |

**What each result does**

| Call result (category) | Next step |
|---|---|
| successful, declined, invalid (`done`) | **Finished for good** |
| no_pickup, busy (`retry`) | Called again on a later day; one attempt used |
| rescheduled (`callback`) | Called on the `reschedual` date, by the same caller. Uses an attempt only if `reschedules_count_as_attempts` is `true` |
| code not in `outcomes` | Treated as retry; *Other* in the report; warning in the log |
| no submission at all | Stays `open` with nothing counted; it can be drawn again |

**max_attempts** (default 5): when a number's attempts reach this and the
result is still not final, it becomes `exhausted`. `ts reopen --exhausted`
gives all of them another round (attempts reset to 0). `0` means no limit.

**The daily draw**

1. Today's slots = the sum of every active caller's quota.
2. Filled from `call_list` numbers that are `open` and whose callback date
   (if any) has come: **callbacks due first, then retries, then pulled numbers
   not yet called**, random within each group.
3. Not enough? New numbers are pulled **at random** from `phone_pool` (only
   numbers not yet in `call_list`): just the shortfall, or `pull.batch_size`
   at a time if you set one. `ts pull 200` pulls by hand.
4. Rescheduled numbers go to the caller who booked them; the rest are spread
   so every caller fills their quota evenly.
5. `assignments` is emptied and refilled with today's draw. The uploaded
   `assignments.csv` holds those not yet called (and still `open`).

---

## 5. Checking on things

| Question | Command |
|---|---|
| How big is the pool, how many are being worked, results so far, today's draw? | `ts status` |
| Are database, KoBo and the form OK? | `ts check` |
| Did `assignments.csv` reach KoBo and the phones? | `.venv/bin/python verify_kobo_media.py` |
| Which codes, callers and statuses does the form produce; are they mapped? | `ts inspect` |
| Per-caller numbers for a day | `ts report --print` or `ts report --date 2026-10-05 --print` |
| Does email work? | `ts test-email` |
| Is the schedule installed, when did the last sync run, is it failing? | `ts schedule` |

### Logs

| Log | Contains |
|---|---|
| `data/telesurvey.log` | Morning runs, syncs, manual commands, email sending |
| `data/launchd-daily.log`, `data/launchd-sync.log` | Mac: anything the scheduled jobs printed (start-up errors) |
| `data/cron.log` | Linux: output of the scheduled jobs |
| PythonAnywhere **Tasks** tab | Output of the scheduled tasks |

```bash
tail -n 50 data/telesurvey.log
grep -E "ERROR|WARNING" data/*.log
```

A healthy morning run logs, in order: `Phone list: …`, `Fetched … submissions`,
`Submissions: {'applied': …}`, `Assigned … numbers`, `Uploaded assignments.csv`,
`Email report sent to …`. A healthy sync logs one line, e.g.
`Sync: 12 submissions checked (…), 12 new results applied; uploaded (41 numbers still to call)`
or `…; list unchanged, not uploaded`. Outside sync hours, or between intervals,
the 5-minute check writes nothing.

---

## 6. Routine changes

Changes to `config.json` and the phone list apply from the next run unless noted.

### Report recipients

```bash
ts set-recipients manager@org.org supervisor@org.org   # replaces the list
ts set-recipients                                       # empty: no more reports
```

or edit `report.recipients` in `config.json`. Mail server details:
`./setup.sh --email`.

### Add phone numbers

Add rows to `data/phone_numbers.csv`; they are added to `phone_pool` (known
numbers are ignored) and pulled into `call_list` when needed. Removing a row
does **not** remove the number; see [Remove a number](#remove-a-number).

### Add, pause or remove a caller

Edit `surveyors` in `config.json`:

- **Add:** a new entry whose `id` is the value the caller enters in `idnumber`.
- **Pause / remove:** `"active": false` (keep the entry so reports still show the name).
- **Change one person's load:** `"daily_quota": 20`.

### A new call-result choice in the form

Add its choice **name** to the right category under `outcomes`, then check with `ts inspect`.

### Numbers per day, attempts

`assignment.numbers_per_surveyor` and `assignment.max_attempts` in `config.json`.

### What each run downloads

Each run downloads every submission received since the **last successful
download**, starting `kobo_import.overlap_hours` (24) earlier as a safety
margin. Submissions already processed are skipped by their `_id`.

| Situation | What happens |
|---|---|
| Normal day | About one day of submissions is downloaded |
| The system didn't run for days or weeks | The next run reaches back to the last success: nothing is missed |
| A run fails | The mark stays where it was; the next run covers the gap |
| First run ever | The last `lookback_days` (14) |
| Older submissions needed once | `./daily.sh --since 2026-01-01` |

The email report shows what was downloaded, e.g. *Submissions checked: 85 (since
the last download (2026-10-05 06:00) minus 24 h overlap)*.

### Schedule

| Change | Command |
|---|---|
| Sync interval | `ts set-refresh 45` (20–360 minutes, or 0 = no syncing during the day) |
| Sync hours | `ts set-schedule --from 08:00 --until 18:00` |
| Morning run time / days | `ts set-schedule --morning 05:30 --days mon,tue,wed,thu,fri,sat` |
| Sync right now | `./sync.sh --now` |
| Pause everything | `./setup.sh --unschedule` (back on with `./setup.sh --schedule`) |

Changes apply from the next 5-minute check; an installed schedule is updated
automatically when the morning time or days change. Every upload redeploys the
form, so very short intervals mean many form versions in KoBo; 30–60 minutes
suits most teams.

### Redraw today's assignment

`./daily.sh --force`. Callers must refresh the form afterwards. Without
`--force`, a second run on the same day keeps the draw and only re-uploads it.

### Remove a number

```bash
ts remove 0700100002 0700100003
```

Sets them to `removed` in `call_list` and takes them off today's list.
They stay in `phone_pool` (which is never changed) and are never pulled again.

### Give numbers another chance

```bash
ts reopen --exhausted          # every exhausted number
ts reopen 0700100002           # specific numbers (also removed or finished ones)
```

Status becomes `open` and attempts go back to 0; they can be drawn from the next run.

### Pull numbers by hand

```bash
ts pull 200                    # 200 random numbers from phone_pool into call_list
```

Normally not needed: the daily run pulls what it needs (see `pull.batch_size`).

### Change max attempts

`assignment.max_attempts` in `config.json` (0 = no limit), and
`assignment.reschedules_count_as_attempts` to decide whether "call me later"
uses up an attempt.

---

## 7. Fixing problems

| Symptom | Likely cause | What to do |
|---|---|---|
| No email arrived | Mail settings, spam folder, or `send_on` is `failure` | `ts test-email`; check spam; `grep "email report" data/telesurvey.log` |
| Calls missing from the report | Wrong date (time zone) or wrong caller ID | Set `TZ` in `.env`; `ts inspect` lists callers |
| A caller appears as a login name | `idnumber` left empty | Remind the caller; the call still counts |
| Numbers stay in `review` | Approval is required and submissions aren't reviewed | Review in KoBo, or set `required_validation_status` to `""` (section 2) |
| Many *Other* results / `Unknown call result` warnings | A code isn't in `outcomes` | `ts inspect`, then add the code |
| Phones show yesterday's numbers | Form not refreshed, upload failed, or form not redeployed | `verify_kobo_media.py`; refresh the form; `./daily.sh` |
| FAILED email: *downloading submissions from KoBo* | KoBo unreachable or token expired | `ts check`, then `./daily.sh` (safe to repeat) |
| Upload FAILED email | KoBo rejected the upload | `./daily.sh` re-uploads the same draw; then `verify_kobo_media.py` |
| FAILED email: *connecting to the database* | Database down or password changed | Fix the database or `./setup.sh --database` |
| A caller sees no numbers | Wrong caller ID entered, or `active: false` | Compare `idnumber` with `config.json` |
| `Submission … has a number that is not in the phone list` | Number not from the list | Callers must pick from the list |
| `phone_pool has no numbers left to pull` / fewer numbers assigned than slots | Every imported number has been pulled | Add numbers to `data/phone_numbers.csv`, or `ts reopen --exhausted` |
| **Sync FAILED** email | KoBo, the database or the internet connection unavailable | `ts check`; fix it. Syncs keep retrying, and you get one "Sync working again" email once it recovers |
| Callers' lists don't shrink during the day | Sync off, outside sync hours, or failing; or the form not refreshed on the phone | `ts schedule`; ask callers to refresh the form |
| Mac: nothing ran this morning | Mac was switched off and nobody logged in, or the schedule isn't installed | Log in (the first sync check does the missed run), or `./daily.sh`; `ts schedule` |

**Nothing is lost if a run fails, the computer is off for a while, or syncs
fail:** the next successful run or sync downloads everything since the last
successful download.

### Results from a KoBo export instead of the API

Export the data as CSV with XML values and headers, including `_id`,
`_submission_time` and `_validation_status`, then:

```bash
./daily.sh --results-csv=/path/to/export.csv
```

---

## 8. Maintenance

### Back up the database

```bash
pg_dump -h HOST -U USER -d telesurvey -Fc -f backup_$(date +%F).dump
```

Restore into an empty database with `pg_restore -h HOST -U USER -d telesurvey backup_DATE.dump`.
Also keep copies of `config.json` and `.env`.

### Database console

```bash
psql -h HOST -U USER -d telesurvey        # asks for the password
```

If you set `PG_SCHEMA`, first run `SET search_path TO yourschema;`. Useful queries:

```sql
-- results per caller for one day
SELECT surveyor_id, outcome, COUNT(*) FROM submissions
WHERE submit_date = '2026-10-05'
GROUP BY 1, 2 ORDER BY 1, 2;

-- submissions not applied yet (only happens when approval is required)
SELECT submit_date, surveyor_id, COUNT(*) FROM submissions
WHERE applied = 0 GROUP BY 1, 2 ORDER BY 1, 2;

-- full history of one number
SELECT * FROM submissions WHERE phone = '0700100002' ORDER BY kobo_id;

-- the numbers being worked, by status and last result
SELECT status, last_result, COUNT(*) FROM call_list GROUP BY 1, 2 ORDER BY 1, 2;

-- numbers contacted on a day, with their latest result
SELECT phone, last_result, attempts, last_caller FROM call_list
WHERE last_contacted_on = '2026-10-05' ORDER BY last_result;

-- exhausted numbers, oldest contact first
SELECT phone, attempts, first_contacted_on, last_contacted_on FROM call_list
WHERE status = 'exhausted' ORDER BY last_contacted_on;
```

### Keep logs from growing forever

On a VPS, create `/etc/logrotate.d/telesurvey`:

```
/home/YOU/telesurvey/data/*.log {
    weekly
    rotate 8
    compress
    missingok
    copytruncate
}
```

### After changing `.env` or updating the code

Nothing to restart: every scheduled run reads `.env` and `config.json` afresh.

### Change passwords and secrets

| What | Steps |
|---|---|
| Mail password | `./setup.sh --email` (or edit `SMTP_PASSWORD` in `.env`), then `ts test-email` |
| Database password | `./setup.sh --database` |
| KoBo token | edit `KOBO_TOKEN` in `.env`, then `ts check` |

### Update to a new version

1. Back up the database, `config.json` and `.env`.
2. Replace `telesurvey/`, `setup.sh`, `daily.sh`, `sync.sh`, `verify_kobo_media.py`,
   `requirements.txt` and the `.md` files. Keep `config.json`, `.env` and `data/`.
3. Run `./setup.sh --schedule` (installs requirements, upgrades the database
   automatically and reinstalls the schedule).

**Coming from the version with the webhook?** Remove what it installed:
in KoBo, form → Settings → REST Services → delete *Telesurvey live updates*;
on a server, `sudo systemctl disable --now telesurvey-webhook` and remove the
`location /kobo/webhook` block from nginx; on PythonAnywhere, delete the web app
and the always-on task. Your old interval setting is carried over.

---

## 9. Command reference

| Command | What it does |
|---|---|
| `./daily.sh` | The morning run, including the email report (by hand it always re-uploads and reports) |
| `./daily.sh --no-upload` | Full run without uploading to KoBo |
| `./daily.sh --no-kobo` | Skip KoBo entirely |
| `./daily.sh --no-report` | Don't send the email |
| `./daily.sh --force` | Throw away today's draw and draw again |
| `./daily.sh --results-csv=FILE` | Use a KoBo CSV export instead of the API |
| `./daily.sh --since YYYY-MM-DD` | One-off: download every submission from that date on |
| `./daily.sh --date YYYY-MM-DD` | Run as if it were another day (testing only) |
| `./sync.sh` | One sync check (what the schedule runs every 5 minutes) |
| `./sync.sh --now` | Sync right away, whatever the time |
| `ts schedule` | Show the schedule, last sync and any failure |
| `ts status` | phone_pool size, call_list by status and result, today's assignment, sync status |
| `ts pull N` | Pull N random numbers from phone_pool into call_list |
| `ts reopen --exhausted` / `ts reopen PHONE…` | Give numbers another chance (open, attempts 0) |
| `ts remove PHONE…` | Stop calling these numbers |
| `ts check` | Test database, KoBo connection and form fields |
| `ts inspect` | Show the form's real call-result codes, callers and statuses, and what is unmapped |
| `ts report [--date D] [--print] [--to ADDR…]` | Send (or print) the per-caller report for a day |
| `ts test-email [--to ADDR…]` | Send a test email |
| `ts set-recipients ADDR…` | Set the report recipients |
| `ts verify` / `verify_kobo_media.py` | Check `assignments.csv` is on KoBo and served to phones |
| `ts refresh` | Upload the list of numbers still to call, now |
| `ts set-refresh N` | Sync interval: 20–360 minutes, or 0 = off |
| `ts set-schedule [--morning T] [--from T] [--until T] [--days D]` | Change the morning time, sync hours or days |
| `ts init` | Create or upgrade tables, show the configuration |
| `ts import-sqlite FILE` | Copy data from an old SQLite database |
| `./setup.sh` | Install or repair; safe to re-run |
| `./setup.sh --email` | Re-enter mail server and recipients |
| `./setup.sh --database` | Re-enter the PostgreSQL details |
| `./setup.sh --schedule` | Install the morning run and sync job (launchd on a Mac, cron on Linux) |
| `./setup.sh --wake` | Mac: wake the Mac 5 minutes before the morning run |
| `./setup.sh --unschedule` | Remove the schedule |
| `./setup.sh --refresh-minutes N` | Set the sync interval during setup |
| `./setup.sh --pythonanywhere` | PythonAnywhere settings and steps |
