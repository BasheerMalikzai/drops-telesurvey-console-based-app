"""The sync job: runs every few minutes during working hours (scheduled by ./setup.sh --schedule).

Each sync:
  1. downloads submissions received since the last download (short overlap),
  2. applies their call results to call_list (exactly like the morning run),
  3. re-uploads assignments.csv to KoBo only if the list of numbers still to call changed.

It never draws numbers and never sends the daily report. It emails only when it starts
failing and when it works again, never once per failed attempt.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from . import db, export, results

log = logging.getLogger(__name__)

UPLOADED_KEY = "uploaded_list"          # meta: "<day>:<md5 of the uploaded file>"
UPLOADED_AT_KEY = "uploaded_list_at"    # meta: unix time of the last upload


# --------------------------------------------------------------------------- state file

def _state_path(cfg) -> Path:
    return cfg.project_dir / "data" / ".sync_state.json"


def load_state(cfg) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(cfg, state: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------- uploading

def upload_list(cfg, day: str, client=None, force: bool = False) -> str:
    """Upload the list of numbers still to call for `day`. Without force, only if it changed.

    Returns a short description of what happened. Raises if the upload itself fails."""
    conn = db.connect(cfg, create_schema=False)
    try:
        media = export.export_remaining(cfg, conn, day, quiet=True)
        if media is None:
            return f"no assignment for {day} yet"
        digest = hashlib.md5(media.read_bytes()).hexdigest()
        marker = f"{day}:{digest}"
        if not force and db.get_meta(conn, UPLOADED_KEY) == marker:
            return "list unchanged, not uploaded"
        if not cfg.kobo_enabled:
            return "list written locally (KoBo not configured)"
        if client is None:
            from .kobo import KoboClient
            client = KoboClient(cfg)
        client.upload_media(media, day)
        conn.lock_writes()
        db.set_meta(conn, UPLOADED_KEY, marker)
        db.set_meta(conn, UPLOADED_AT_KEY, int(time.time()))
        conn.commit()
        remaining = sum(1 for _ in open(media, encoding="utf-8")) - 1
        return (f"uploaded ({remaining} numbers still to call)"
                + (" and form redeployed" if cfg.redeploy else ""))
    finally:
        conn.close()


def mark_synced(cfg, what: str) -> None:
    state = load_state(cfg)
    _recovered(cfg, state)
    state.update(last_success=_now().isoformat(timespec="seconds"), last_success_ts=time.time(),
                 last_result=what)
    save_state(cfg, state)


# --------------------------------------------------------------------------- timing

def _now() -> dt.datetime:
    return dt.datetime.now()


def is_run_day(cfg, when: Optional[dt.datetime] = None) -> bool:
    from .config import DAY_NAMES
    return DAY_NAMES[(when or _now()).weekday()] in cfg.run_days


def within_sync_hours(cfg, when: Optional[dt.datetime] = None) -> bool:
    hhmm = (when or _now()).strftime("%H:%M")
    return is_run_day(cfg, when) and cfg.sync_from <= hhmm <= cfg.sync_until


def morning_is_due(cfg, conn, today: str, when: Optional[dt.datetime] = None) -> bool:
    """True if the morning run should have happened today but hasn't (e.g. the Mac was off)."""
    when = when or _now()
    if not is_run_day(cfg, when) or when.strftime("%H:%M") < cfg.morning_time:
        return False
    n = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE assign_date=?", (today,)).fetchone()["c"]
    return n == 0


def skip_reason(cfg, state: dict, now_ts: float) -> Optional[str]:
    """Why a scheduled sync should do nothing right now (None = go ahead)."""
    if cfg.sync_every_minutes == 0:
        return "syncing is switched off (schedule.sync_every_minutes = 0)"
    if not within_sync_hours(cfg):
        return (f"outside sync hours ({cfg.sync_from}-{cfg.sync_until} on "
                f"{', '.join(cfg.run_days)})")
    last = float(state.get("last_success_ts") or 0)
    # 2 minutes of slack, so a job scheduled exactly on the interval isn't skipped
    if now_ts - last < cfg.sync_every_minutes * 60 - 120:
        return f"last sync was {int((now_ts - last) // 60)} min ago (interval {cfg.sync_every_minutes} min)"
    return None


# --------------------------------------------------------------------------- alerts

def _alert(cfg, subject: str, text: str) -> None:
    if not cfg.report_enabled or cfg.report_send_on == "never" or not cfg.email_ready:
        return
    from .report import send_email
    try:
        send_email(cfg, f"{cfg.report_subject_prefix} {subject}", text)
        log.info("Alert email sent: %s", subject)
    except Exception as e:
        log.error("Could not send the alert email: %s", e)


def _failed(cfg, state: dict, step: str, exc: BaseException) -> None:
    import re
    err = re.sub(r" for url: \S+", "", f"{type(exc).__name__}: {exc}")[:500]
    first = not state.get("failing")
    state.update(failing=True, failing_since=state.get("failing_since") or _now().isoformat(timespec="minutes"),
                 last_error=err, last_step=step, failures=int(state.get("failures") or 0) + 1)
    save_state(cfg, state)
    if first:
        _alert(cfg, "Sync FAILED", "\n".join([
            f"The sync job failed at {_now():%Y-%m-%d %H:%M} while {step}.", "",
            f"Error: {err}", "",
            "Callers' lists are not being refreshed and new results are not being applied until this",
            "is fixed. Nothing is lost: the next successful sync catches up on everything.",
            "You will get one more email when syncing works again (not one per failed attempt).", "",
            "Check: .venv/bin/python -m telesurvey check",
            "Log:   data/telesurvey.log"]))


def _recovered(cfg, state: dict) -> None:
    if state.get("failing"):
        _alert(cfg, "Sync working again", "\n".join([
            f"The sync job is working again ({_now():%Y-%m-%d %H:%M}).",
            f"It had been failing since {state.get('failing_since')} "
            f"({state.get('failures')} failed attempts; last error: {state.get('last_error')}).",
            "Everything submitted meanwhile has now been applied."]))
    for k in ("failing", "failing_since", "last_error", "last_step", "failures"):
        state.pop(k, None)


# --------------------------------------------------------------------------- the job

def run_sync(cfg, today: str, force: bool = False) -> int:
    """One sync. Returns 0 on success or skip, 1 on failure."""
    state = load_state(cfg)
    if not force:
        reason = skip_reason(cfg, state, time.time())
        if reason:
            log.debug("Sync skipped: %s", reason)
            return 0

    step = "waiting for the daily run to finish"
    try:
        with db.run_lock(cfg, wait_seconds=0) as got:
            if not got:
                log.info("Sync postponed: the daily run is in progress")
                return 0
            step = "connecting to the database"
            conn = db.connect(cfg)
            try:
                conn.lock_writes()
                applied = 0
                if cfg.kobo_enabled:
                    step = "downloading submissions from KoBo"
                    from .kobo import KoboClient
                    client = KoboClient(cfg)
                    since, window = results.download_window(cfg, conn, today,
                                                            overlap_minutes=cfg.sync_overlap_minutes)
                    started = results.now_utc()
                    records = client.fetch_submissions(since)
                    step = "applying call results"
                    counts = results.process_results(cfg, conn, records, today)
                    results.expire_old_reviews(cfg, conn, today)
                    db.set_meta(conn, results.LAST_FETCH_KEY, started)
                    applied = counts.get("applied", 0)
                else:
                    client, window, records = None, "KoBo not configured", []
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

            step = "uploading the call list to KoBo"
            upload = upload_list(cfg, today, client=client)
    except Exception as e:
        log.exception("Sync failed while %s", step)
        _failed(cfg, state, step, e)
        return 1

    log.info("Sync: %d submissions checked (%s), %d new results applied; %s",
             len(records), window, applied, upload)
    mark_synced(cfg, f"{applied} new results; {upload}")
    return 0
