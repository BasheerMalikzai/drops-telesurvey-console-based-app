"""Command line: python -m telesurvey {init,check,run,sync,status,schedule,...} (see --help)"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from . import assign, db, export, phones, results
from .config import (REFRESH_MAX_MINUTES, REFRESH_MIN_MINUTES, ConfigError, check_refresh_minutes,
                     load_config, save_refresh_minutes)
from .db import DatabaseError

log = logging.getLogger("telesurvey")


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()])


def cmd_init(cfg, args) -> int:
    conn = db.connect(cfg)  # creates the tables if they don't exist
    conn.close()
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.phone_list.exists():
        cfg.phone_list.parent.mkdir(parents=True, exist_ok=True)
        cfg.phone_list.write_text("phone,name\n", encoding="utf-8")
        print(f"  Created empty phone list: {cfg.phone_list} (add your numbers)")
    active = cfg.active_surveyors()
    print(f"  Database:    {cfg.db_description} (connected, tables ready)")
    print(f"  Surveyors:   {len(active)} active of {len(cfg.surveyors)} in config.json")
    for s in cfg.surveyors:
        state = "active" if s in active else "inactive"
        print(f"    {s.id:<8} {s.name:<20} {s.phone:<16} quota {cfg.quota(s):<4} {state}")
    print(f"  KoBo:        {'configured' if cfg.kobo_enabled else 'NOT configured (set KOBO_TOKEN and KOBO_ASSET_UID in .env)'}")
    return 0


def cmd_check(cfg, args) -> int:
    try:
        db.connect(cfg).close()
        print(f"  Database OK: {cfg.db_description}")
    except DatabaseError as e:
        print(f"  Database problem: {e}")
        return 1
    if not cfg.kobo_enabled:
        print("  KoBo is not configured: set KOBO_TOKEN and KOBO_ASSET_UID in .env")
        return 1
    from .kobo import KoboClient
    try:
        problems = KoboClient(cfg).check()
    except Exception as e:
        print(f"  Could not reach KoBo: {e}")
        return 1
    for p in problems:
        print(f"  WARNING: {p}")
    print("  KoBo connection OK" if not problems else "  KoBo reachable, but see warnings above")
    return 0 if not problems else 1


def cmd_run(cfg, args) -> int:
    from .report import RunInfo, send_run_report
    info = RunInfo(day=args.date, options=[a for a in sys.argv[1:] if a.startswith("--")])
    info.step = "connecting to the database"
    catch_up = getattr(args, "catch_up", False)       # started by a sync: never wait
    scheduled = getattr(args, "scheduled", False)     # started by the schedule
    try:
        with db.run_lock(cfg, wait_seconds=0 if catch_up else 600) as got:
            if not got:
                if catch_up:
                    log.info("The morning run is already running; not starting a second one")
                    return 0
                info.step = "waiting for another run to finish"
                raise RuntimeError("Another run or sync has held the lock for 10 minutes")
            if (catch_up or scheduled) and _drawn_today(cfg, args.date):
                # e.g. the Mac woke up and both jobs started: the other one already did today's run
                log.info("Today's run already happened (%s); nothing to do", args.date)
                return 0
            status = _run_locked(cfg, args, info)
    except Exception as e:
        log.exception("Run failed")
        if info.status == "running":
            info.fail(e)
        status = 1
    if info.status == "running":
        info.status = "ok"
    if info.status == "ok" and status == 0 and not args.no_kobo and not args.results_csv:
        from .sync import mark_synced
        mark_synced(cfg, "daily run")   # the morning run counts as the first sync of the day
    if not args.no_report:
        send_run_report(cfg, info)
    return status


def _drawn_today(cfg, day: str) -> bool:
    conn = db.connect(cfg, create_schema=False)
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE assign_date=?",
                            (day,)).fetchone()["c"] > 0
    finally:
        conn.close()


def _run_locked(cfg, args, info) -> int:
    today = args.date
    info.step = "connecting to the database"
    conn = db.connect(cfg)
    use_kobo = cfg.kobo_enabled and not args.no_kobo
    client = None
    downloaded, fetch_started = False, None
    try:
        conn.lock_writes()
        info.step = "importing the phone list"
        info.imported = phones.import_phone_list(cfg, conn, today)
        if args.results_csv:
            info.step = "reading the results CSV"
            records = results.load_csv_results(Path(args.results_csv))
            info.window = f"from {Path(args.results_csv).name}"
        elif use_kobo:
            info.step = "downloading submissions from KoBo"
            from .kobo import KoboClient
            client = KoboClient(cfg)
            since, info.window = results.download_window(cfg, conn, today, args.since)
            fetch_started = results.now_utc()
            records = client.fetch_submissions(since)
            downloaded = True
        else:
            log.warning("KoBo not configured or skipped: no results pulled today")
            records = []
        info.fetched = len(records)
        info.step = "applying call results"
        info.counts = results.process_results(cfg, conn, records, today)
        info.expired = results.expire_old_reviews(cfg, conn, today)
        info.step = "drawing today's assignment"
        before = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE assign_date=?",
                              (today,)).fetchone()["c"]
        info.assigned, info.pulled = assign.build_today(cfg, conn, today, force=args.force)
        info.reused = bool(before) and not args.force
        if downloaded:
            # saved in the same transaction: a run that fails leaves the mark where it was
            db.set_meta(conn, results.LAST_FETCH_KEY, fetch_started)
        conn.commit()
        info.db_changed = True
    except ConfigError:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("Run failed while %s; database left unchanged", info.step)
        info.fail(e)
        conn.close()
        return 1

    info.step = "writing the assignment file"
    media = export.export_csv(cfg, conn, today)
    conn.close()
    status = 0
    if not media:
        info.upload = "nothing to upload (no numbers assigned)"
    elif not use_kobo:
        info.upload = "skipped (KoBo not configured or --no-kobo)"
    elif args.no_upload:
        info.upload = "skipped (--no-upload)"
    else:
        info.step = "uploading the assignment to KoBo"
        try:
            from .sync import upload_list
            info.upload = upload_list(cfg, today, client=client, force=True)
        except Exception as e:
            log.exception("Upload failed. The assignment is saved; rerun to retry the upload.")
            info.fail(e)
            info.status = "upload_failed"
            info.upload = f"FAILED: {e}"[:300]
            status = 2
    conn = db.connect(cfg, create_schema=False)
    export.print_status(cfg, conn, today)
    conn.close()
    return status


def cmd_report(cfg, args) -> int:
    from . import report
    today = dt.date.today().isoformat()
    day = args.report_date or report.report_date_for(cfg, today)
    conn = db.connect(cfg)
    try:
        subject, text, html_body = report.build(cfg, conn, None, day, today)
    finally:
        conn.close()
    if args.print:
        print(subject)
        print("=" * len(subject))
        print(text)
        return 0
    try:
        report.send_email(cfg, subject, text, html_body, recipients=args.to or None)
    except Exception as e:
        print(f"  Could not send: {e}")
        return 1
    print(f"  Report for {day} sent to {', '.join(args.to or cfg.report_recipients)}")
    return 0


def cmd_test_email(cfg, args) -> int:
    from .report import send_email
    to = args.to or cfg.report_recipients
    smtp = cfg.smtp
    print(f"  Server:     {smtp.get('host') or '(SMTP_HOST not set)'}:{smtp.get('port') or 'default'} "
          f"({smtp.get('security')})")
    print(f"  From:       {smtp.get('sender') or smtp.get('user') or '(SMTP_FROM not set)'}")
    print(f"  Recipients: {', '.join(to) or '(report.recipients is empty)'}")
    try:
        send_email(cfg, f"{cfg.report_subject_prefix} Test email",
                   "This is a test from the telesurvey tool. Daily reports will arrive like this.",
                   recipients=to)
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1
    print("  Test email sent. Check the inboxes (and spam folders).")
    return 0


def cmd_set_recipients(cfg, args) -> int:
    from .config import save_recipients
    emails = [e for a in args.emails for e in a.replace(";", ",").split(",")]
    try:
        saved = save_recipients(cfg, emails)
    except ConfigError as e:
        print(f"  {e}")
        return 1
    print(f"  Report recipients: {', '.join(saved) or '(none: reports switched off)'}")
    return 0


def cmd_pull(cfg, args) -> int:
    from .assign import pull_numbers
    conn = db.connect(cfg)
    try:
        conn.lock_writes()
        got = pull_numbers(cfg, conn, args.count, dt.date.today().isoformat())
        conn.commit()
    finally:
        conn.close()
    print(f"  {len(got)} numbers pulled from phone_pool into call_list")
    return 0


def _phones_arg(values):
    return [phones.norm_phone(v) for v in values if phones.norm_phone(v)]


def cmd_reopen(cfg, args) -> int:
    today = dt.date.today().isoformat()
    conn = db.connect(cfg)
    try:
        conn.lock_writes()
        if args.exhausted:
            n = conn.execute("UPDATE call_list SET status='open', attempts=0, updated_on=? WHERE status='exhausted'",
                             (today,)).rowcount
        else:
            nums = _phones_arg(args.phones)
            if not nums:
                print("  Give phone numbers, or --exhausted")
                return 1
            n = 0
            for ph in nums:
                n += conn.execute("UPDATE call_list SET status='open', attempts=0, callback_date=NULL, updated_on=? "
                                  "WHERE phone=?", (today, ph)).rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"  {n} numbers reopened (attempts reset to 0); they can be drawn from the next run")
    return 0


def cmd_remove(cfg, args) -> int:
    today = dt.date.today().isoformat()
    nums = _phones_arg(args.phones)
    if not nums:
        print("  Give one or more phone numbers")
        return 1
    conn = db.connect(cfg)
    try:
        conn.lock_writes()
        n = 0
        for ph in nums:
            if conn.execute("SELECT 1 FROM phone_pool WHERE phone=?", (ph,)).fetchone() is None:
                print(f"  {ph}: not in phone_pool, skipped")
                continue
            conn.execute("""INSERT INTO call_list(phone, pulled_on, status, updated_on) VALUES(?,?,'removed',?)
                            ON CONFLICT(phone) DO UPDATE SET status='removed', updated_on=excluded.updated_on""",
                         (ph, today, today))
            conn.execute("DELETE FROM assignments WHERE phone=?", (ph,))
            n += 1
        conn.commit()
    finally:
        conn.close()
    print(f"  {n} numbers removed from calling (they stay in phone_pool)")
    return 0


def cmd_inspect(cfg, args) -> int:
    if not cfg.kobo_enabled:
        print("  KoBo is not configured: set KOBO_TOKEN and KOBO_ASSET_UID in .env")
        return 1
    from .inspect_form import run
    return run(cfg, args.date)


def cmd_refresh(cfg, args) -> int:
    """Upload the list of numbers still to call, right now."""
    from .sync import upload_list
    with db.run_lock(cfg, wait_seconds=120) as got:
        if not got:
            print("  The daily run or a sync is in progress; try again in a minute")
            return 1
        try:
            print(f"  {upload_list(cfg, args.date, force=True)}")
        except Exception as e:
            log.error("Upload failed: %s", e)
            return 1
    return 0


def cmd_sync(cfg, args) -> int:
    from . import sync
    # Backup for a missed morning run (the Mac was switched off at the scheduled time):
    # the first sync after the morning time does the full daily run instead.
    if not args.no_catch_up:
        try:
            conn = db.connect(cfg, create_schema=False)
            try:
                due = sync.morning_is_due(cfg, conn, args.date)
            finally:
                conn.close()
        except Exception:
            due = False  # the sync below reports the database problem
        if due:
            log.info("No assignment for %s yet and the morning run time (%s) has passed: "
                     "running the daily run now", args.date, cfg.morning_time)
            run_args = argparse.Namespace(date=args.date, no_upload=False, no_kobo=False, results_csv=None,
                                          force=False, no_report=False, since=None, catch_up=True,
                                          scheduled=False)
            return cmd_run(cfg, run_args)
    return sync.run_sync(cfg, args.date, force=args.now)


def cmd_set_refresh(cfg, args) -> int:
    try:
        minutes = save_refresh_minutes(cfg, args.minutes)
    except ConfigError as e:
        print(f"  {e}")
        return 1
    where = cfg.config_path or "config.json"
    if minutes:
        print(f"  Sync: every {minutes} minutes, {cfg.sync_from}-{cfg.sync_until} (saved in {where})")
    else:
        print(f"  Sync during the day: off (saved in {where}); the morning run still runs")
    print("  Takes effect at the next scheduled check (within 5 minutes); nothing to reinstall.")
    return 0


def cmd_set_schedule(cfg, args) -> int:
    from .config import save_schedule
    try:
        save_schedule(cfg, morning=args.morning, sync_from=args.sync_from, sync_until=args.sync_until,
                      days=args.days)
    except ConfigError as e:
        print(f"  {e}")
        return 1
    print(f"  Morning run {cfg.morning_time} on {', '.join(cfg.run_days)}; "
          f"sync hours {cfg.sync_from}-{cfg.sync_until} (saved in config.json)")
    from . import schedule
    if (args.morning or args.days) and schedule.installed(cfg):
        print("  " + schedule.install(cfg))   # the morning time is part of the installed job
    return 0


def cmd_schedule(cfg, args) -> int:
    from . import schedule
    try:
        if args.action == "install":
            print("  " + schedule.install(cfg))
        elif args.action == "remove":
            print("  " + schedule.remove(cfg))
        else:
            print(schedule.describe(cfg))
    except schedule.ScheduleError as e:
        print(f"  {e}")
        return 1
    return 0


def cmd_verify(cfg, args) -> int:
    from .verify import run
    return run(cfg, day=args.date,
               local=Path(args.local) if args.local else None,
               save_copy=Path(args.save_copy) if args.save_copy else None,
               check_db=not args.skip_db, check_phones=not args.skip_phones)


def cmd_status(cfg, args) -> int:
    conn = db.connect(cfg)
    export.print_status(cfg, conn, args.date)
    conn.close()
    return 0


def cmd_import_sqlite(cfg, args) -> int:
    from pathlib import Path as _P
    src = _P(args.path)
    if not src.is_absolute():
        src = cfg.project_dir / src
    if not src.exists():
        print(f"  {src} not found")
        return 1
    if cfg.db_engine != "postgres":
        print("  DB_ENGINE is not 'postgres' in .env; nothing to import into")
        return 1
    copied = db.copy_sqlite_to(cfg, src)
    for table, n in copied.items():
        print(f"  {table:<12} {n} rows copied")
    print(f"  Into: {cfg.db_description}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="telesurvey", description="Daily telesurvey number assignment")
    parser.add_argument("--config", help="path to config.json (default: project folder)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and folders, show the configuration")
    sub.add_parser("check", help="test the KoBo connection and form fields")
    syn = sub.add_parser("sync", help="download new results and refresh the call list (scheduled job)")
    syn.add_argument("--now", action="store_true",
                     help="sync even outside sync hours / before the interval has passed")
    syn.add_argument("--no-catch-up", action="store_true",
                     help="don't do a missed morning run")
    ss = sub.add_parser("set-schedule", help="change the morning run time, sync hours or run days")
    ss.add_argument("--morning", metavar="HH:MM", help="time of the morning run, e.g. 06:00")
    ss.add_argument("--from", dest="sync_from", metavar="HH:MM", help="first sync of the day, e.g. 07:00")
    ss.add_argument("--until", dest="sync_until", metavar="HH:MM", help="last sync of the day, e.g. 19:00")
    ss.add_argument("--days", help="run days, e.g. mon,tue,wed,thu,fri,sat")
    sch = sub.add_parser("schedule", help="install, remove or show the morning run and sync schedule")
    sch.add_argument("action", nargs="?", choices=["install", "remove", "show"], default="show")
    imp = sub.add_parser("import-sqlite", help="copy data from an old SQLite database into PostgreSQL")
    imp.add_argument("path", nargs="?", default="data/telesurvey.db", help="SQLite file (default: data/telesurvey.db)")

    run = sub.add_parser("run", help="process results and build today's assignment")
    run.add_argument("--no-upload", action="store_true", help="don't upload the file to KoBo")
    run.add_argument("--no-kobo", action="store_true", help="skip KoBo entirely")
    run.add_argument("--results-csv", help="read outcomes from a KoBo CSV export instead of the API")
    run.add_argument("--force", action="store_true", help="rebuild today's assignment")
    run.add_argument("--no-report", action="store_true", help="don't send the email report")
    run.add_argument("--scheduled", action="store_true",
                     help="started by the schedule: do nothing if today's run already happened")
    run.add_argument("--since", metavar="YYYY-MM-DD",
                     help="download every submission from this date on (one-off backfill)")

    pl = sub.add_parser("pull", help="pull N random numbers from phone_pool into call_list now")
    pl.add_argument("count", type=int)
    ro = sub.add_parser("reopen", help="give numbers another chance (status open, attempts 0)")
    ro.add_argument("phones", nargs="*", help="phone numbers to reopen")
    ro.add_argument("--exhausted", action="store_true", help="reopen every exhausted number")
    rm = sub.add_parser("remove", help="stop calling these numbers (phone_pool is left as it is)")
    rm.add_argument("phones", nargs="+")
    insp = sub.add_parser("inspect", help="show the codes, callers and statuses your form really produces")
    rpt = sub.add_parser("report", help="send (or --print) the per-caller report for a day")
    rpt.add_argument("--print", action="store_true", help="print instead of emailing")
    rpt.add_argument("--date", dest="report_date", default=None, help="day of calls to report (default: yesterday)")
    rpt.add_argument("--to", nargs="+", help="send to these addresses instead of report.recipients")
    te = sub.add_parser("test-email", help="send a test email to check the mail settings")
    te.add_argument("--to", nargs="+", help="send to these addresses instead of report.recipients")
    sr = sub.add_parser("set-recipients", help="set the report email addresses (saved in config.json)")
    sr.add_argument("emails", nargs="*", help="one or more addresses (none = switch reports off)")

    refresh = sub.add_parser("refresh", help="upload the list of numbers still to call, right now")

    ver = sub.add_parser("verify", help="check that assignments.csv reached KoBo and works as a form attachment")
    ver.add_argument("--local", help="local file to compare with (default: data/assignments/assignments.csv)")
    ver.add_argument("--save-copy", metavar="FILE", help="save the file downloaded from KoBo here")
    ver.add_argument("--skip-db", action="store_true", help="don't compare with the database")
    ver.add_argument("--skip-phones", action="store_true", help="don't check what KoboCollect receives")

    setr = sub.add_parser("set-refresh",
                          help=f"set how often the sync job runs ({REFRESH_MIN_MINUTES}-{REFRESH_MAX_MINUTES} min, 0 = off)")
    setr.add_argument("minutes", help=f"{REFRESH_MIN_MINUTES} to {REFRESH_MAX_MINUTES}, or 0 to turn it off")
    for p in (run, sub.add_parser("status", help="show the pool and today's assignment"), refresh, ver,
              insp, syn):
        p.add_argument("--date", default=None, help="run as if it were this date (YYYY-MM-DD)")

    args = parser.parse_args(argv)
    if getattr(args, "since", None):
        try:
            dt.date.fromisoformat(args.since)
        except ValueError:
            parser.error(f"--since must be a date like 2026-01-31, not {args.since!r}")
    try:
        cfg = load_config(Path(args.config) if args.config else None)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    # resolved after load_config(), which applies TZ from .env
    if getattr(args, "date", "unset") is None:
        args.date = dt.date.today().isoformat()

    # the always-on receiver keeps its own log so the daily log stays readable
    setup_logging(cfg.log_file)

    handlers = {"init": cmd_init, "check": cmd_check, "run": cmd_run, "status": cmd_status,
                "sync": cmd_sync, "schedule": cmd_schedule, "set-schedule": cmd_set_schedule, "refresh": cmd_refresh,
                "import-sqlite": cmd_import_sqlite,
                "set-refresh": cmd_set_refresh, "verify": cmd_verify, "inspect": cmd_inspect,
                "report": cmd_report, "test-email": cmd_test_email, "set-recipients": cmd_set_recipients,
                "pull": cmd_pull, "reopen": cmd_reopen, "remove": cmd_remove}
    try:
        return handlers[args.command](cfg, args)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        return 1
    except DatabaseError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
