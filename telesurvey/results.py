"""Applying call results from KoBo to the number pool.

By default every submission counts, whatever its validation status.

Optionally (kobo_import.required_validation_status = "validation_status_approved")
only approved submissions change a number's result: until a submission is
reviewed its number is held ('review') so nobody calls it again, and if it is
rejected the number returns to the pool. Each daily run looks back
`kobo_import.lookback_days`, so late reviews and late-synced forms are picked up.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import VALIDATION_APPROVED, VALIDATION_REJECTED
from .db import CLOSED_STATUSES, DONE, EXHAUSTED, OPEN, REVIEW, get_meta
from .kobo import get_field
from .phones import norm_phone, read_csv

log = logging.getLogger(__name__)

# Labels used by KoBo's CSV export, mapped to the codes used by the API
VALIDATION_LABELS = {"approved": VALIDATION_APPROVED, "not approved": VALIDATION_REJECTED,
                     "on hold": "validation_status_on_hold"}
AWAITING = "awaiting"   # stored for submissions not reviewed yet
HOLD_OUTCOME = "awaiting_review"


def load_csv_results(path: Path) -> List[Dict[str, Any]]:
    """Read a KoBo CSV export (must include the _id column)."""
    rows = read_csv(path)
    for r in rows:
        r["_id"] = int(r.get("_id") or 0)
    rows = [r for r in rows if r["_id"]]
    log.info("Read %d submissions from %s", len(rows), path)
    return rows


def validation_of(rec: Dict[str, Any], cfg) -> str:
    """'validation_status_approved', 'validation_status_not_approved', ... or 'awaiting'."""
    v = get_field(rec, cfg.field_validation)
    if isinstance(v, dict):
        v = v.get("uid") or ""
    v = str(v or "").strip().lower()
    v = VALIDATION_LABELS.get(v, v)
    return v or AWAITING


def submission_local_time(rec: Dict[str, Any], cfg) -> Tuple[Optional[str], Optional[str]]:
    """KoBo's _submission_time is UTC. Returns (local ISO time, local date)."""
    raw = get_field(rec, cfg.field_submission_time)
    if not raw:
        return None, None
    text = str(raw).strip().replace("Z", "").replace(" ", "T")
    try:
        t = dt.datetime.fromisoformat(text[:26])
    except ValueError:
        return str(raw), str(raw)[:10]
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    local = t.astimezone()  # TZ from .env, or the server's time zone
    return local.isoformat(timespec="seconds"), local.date().isoformat()


def classify(cfg, validation: str) -> str:
    """approved | rejected | awaiting, given the configured required status."""
    if not cfg.required_validation:  # "" = every submission counts, validation is ignored
        return "approved"
    if validation == cfg.required_validation:
        return "approved"
    if validation == VALIDATION_REJECTED:
        return "rejected"
    return "awaiting"


def caller_of(rec: Dict[str, Any], cfg) -> str:
    value = get_field(rec, cfg.field_surveyor)
    if value in (None, "") and cfg.field_surveyor_fallback:
        value = get_field(rec, cfg.field_surveyor_fallback)
    return str(value or "").strip()


# --------------------------------------------------------------------------- call_list rows

def _row(conn, phone: str, today: str, create: bool = True):
    """The number's call_list row. A number called without having been pulled (e.g. typed in)
    is pulled now, as long as it exists in phone_pool. phone_pool itself is never changed."""
    r = conn.execute("SELECT * FROM call_list WHERE phone=?", (phone,)).fetchone()
    if r is not None or not create:
        return r
    if conn.execute("SELECT 1 FROM phone_pool WHERE phone=?", (phone,)).fetchone() is None:
        return None
    conn.execute("INSERT INTO call_list(phone, pulled_on, status, updated_on) VALUES(?,?,?,?)",
                 (phone, today, OPEN, today))
    return conn.execute("SELECT * FROM call_list WHERE phone=?", (phone,)).fetchone()


def _mark_called(conn, phone: str, day: str) -> None:
    """Today's list drops a number once it has been called on the day it was assigned
    (a late-synced form from an earlier day doesn't remove today's assignment)."""
    conn.execute("UPDATE assignments SET called=1 WHERE phone=? AND assign_date=?", (phone, day))


def _hold(conn, phone: str, today: str, day: Optional[str] = None) -> bool:
    """Approval mode only: keep a number out of the draw while its submission awaits review."""
    r = _row(conn, phone, today)
    _mark_called(conn, phone, day or today)
    if r is None or r["status"] in CLOSED_STATUSES or r["status"] == REVIEW:
        return False
    conn.execute("UPDATE call_list SET status=?, updated_on=? WHERE phone=?", (REVIEW, today, phone))
    return True


def _release(conn, phone: str, today: str, exclude_id: int) -> bool:
    """Undo a hold when no other submission for the number is still awaiting review."""
    other = conn.execute("""SELECT COUNT(*) AS n FROM submissions
                            WHERE phone=? AND applied=0 AND validation NOT IN (?, ?) AND kobo_id<>?""",
                         (phone, VALIDATION_REJECTED, "expired", exclude_id)).fetchone()["n"]
    if other:
        return False
    r = _row(conn, phone, today, create=False)
    if r is None or r["status"] != REVIEW:
        return False
    conn.execute("UPDATE call_list SET status=?, updated_on=? WHERE phone=?", (OPEN, today, phone))
    conn.execute("UPDATE assignments SET called=0 WHERE phone=?", (phone,))
    return True


def _apply(cfg, conn, phone: str, code: str, caller: str, rec, kid: int, today: str,
           local_date: Optional[str], local_time: Optional[str]) -> bool:
    """Record a call result on the number's call_list row."""
    r = _row(conn, phone, today)
    _mark_called(conn, phone, local_date or today)
    if r is None:
        log.warning("Submission %s has a number that is not in the phone list (%s); not applied", kid, phone)
        return False
    if r["status"] in CLOSED_STATUSES:
        return False  # finished numbers stay finished

    category = cfg.category_of(code)
    action = cfg.categories[category]["action"] if category in cfg.categories else "retry"
    if category == "other":
        log.warning("Unknown call result '%s' (submission %s); treating as retry. "
                    "Add it to 'outcomes' in config.json", code, kid)

    counts = action != "callback" or cfg.reschedules_count_as_attempts
    attempts = r["attempts"] + (1 if counts else 0)
    if action == "done":
        status = DONE
    elif cfg.max_attempts and attempts >= cfg.max_attempts:
        status = EXHAUSTED
        log.info("Number %s reached max_attempts (%d) and will not be called again", phone, cfg.max_attempts)
    else:
        status = OPEN

    callback = None
    if action == "callback":
        cb = get_field(rec, cfg.field_callback)
        callback = str(cb)[:10] if cb else None

    day = local_date or today
    first = min(filter(None, [r["first_contacted_on"], day]))
    newest = (r["last_contacted_on"] or "") <= day
    conn.execute("""UPDATE call_list SET status=?, attempts=?, last_result=?, last_result_code=?,
                    last_caller=COALESCE(NULLIF(CAST(? AS TEXT), ''), last_caller),
                    first_contacted_on=?, last_contacted_on=?, last_contacted_at=?,
                    callback_date=?, updated_on=? WHERE phone=?""",
                 (status, attempts, category, code, caller, first,
                  day if newest else r["last_contacted_on"],
                  (local_time or day) if newest else r["last_contacted_at"],
                  callback, today, phone))
    return True


# --------------------------------------------------------------------------- main entry

def process_results(cfg, conn, records: Iterable[Dict[str, Any]], today: str, **_ignored) -> Counter:
    """Record every submission; apply results of approved ones; hold / release the rest.

    Returns counts: 'applied', 'applied:<code>', 'held', 'released', 'awaiting', 'skipped'.
    """
    counts: Counter = Counter()
    for rec in sorted(records, key=lambda r: int(r["_id"])):
        kid = int(rec["_id"])
        phone = norm_phone(get_field(rec, cfg.field_phone))
        outcome = str(get_field(rec, cfg.field_status) or "").strip().lower()
        caller = caller_of(rec, cfg)
        validation = validation_of(rec, cfg)
        local_time, local_date = submission_local_time(rec, cfg)
        if not phone or not outcome:
            log.warning("Submission %s has no phone number or call result; skipped", kid)
            counts["skipped"] += 1
            continue

        prev = conn.execute("SELECT validation, applied FROM submissions WHERE kobo_id=?", (kid,)).fetchone()
        if prev is None:
            conn.execute("""INSERT INTO submissions(kobo_id, phone, surveyor_id, outcome, submitted_at,
                                                    submit_date, validation, applied)
                            VALUES(?,?,?,?,?,?,?,0)""",
                         (kid, phone, caller, outcome, local_time, local_date, validation))
        else:
            if prev["applied"]:
                continue  # already applied; nothing can change it now
            conn.execute("""UPDATE submissions SET phone=?, surveyor_id=?, outcome=?, submitted_at=?,
                            submit_date=?, validation=? WHERE kobo_id=?""",
                         (phone, caller, outcome, local_time, local_date, validation, kid))

        state = classify(cfg, validation)
        if state == "approved":
            applied = _apply(cfg, conn, phone, outcome, caller, rec, kid, today, local_date, local_time)
            conn.execute("UPDATE submissions SET applied=1 WHERE kobo_id=?", (kid,))
            if applied:
                counts["applied"] += 1
                counts[f"applied:{outcome}"] += 1
        elif state == "rejected":
            if prev is None or prev["validation"] != validation:
                if _release(conn, phone, today, kid):
                    counts["released"] += 1
                    log.info("Submission %s was rejected; number %s returns to the pool", kid, phone)
        else:
            counts["awaiting"] += 1
            if _hold(conn, phone, today, local_date):
                counts["held"] += 1

    shown = {k: v for k, v in counts.items() if not k.startswith("applied:")}
    if shown:
        log.info("Submissions: %s", shown)
    return counts


def expire_old_reviews(cfg, conn, today: str) -> int:
    """Submissions still unreviewed after lookback_days are no longer fetched: release their numbers."""
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=cfg.lookback_days)).isoformat()
    old = conn.execute("""SELECT kobo_id, phone FROM submissions
                          WHERE applied=0 AND validation NOT IN (?, ?) AND submit_date < ?""",
                       (VALIDATION_REJECTED, "expired", cutoff)).fetchall()
    for r in old:
        conn.execute("UPDATE submissions SET validation='expired' WHERE kobo_id=?", (r["kobo_id"],))
        _release(conn, r["phone"], today, r["kobo_id"])
    if old:
        log.warning("%d submissions were not reviewed within %d days; their numbers return to the pool",
                    len(old), cfg.lookback_days)
    return len(old)


KOBO_TIME = "%Y-%m-%dT%H:%M:%S"
LAST_FETCH_KEY = "last_fetch_utc"


def _local_midnight_utc(day: dt.date) -> str:
    """Start of a local day, as a UTC timestamp string (KoBo stores _submission_time in UTC)."""
    return dt.datetime.combine(day, dt.time.min).astimezone().astimezone(dt.timezone.utc).strftime(KOBO_TIME)


def fetch_since(cfg, today: str) -> str:
    """Start of the look-back window: local midnight, lookback_days before today, in UTC."""
    return _local_midnight_utc(dt.date.fromisoformat(today) - dt.timedelta(days=cfg.lookback_days))


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(KOBO_TIME)


def download_window(cfg, conn, today: str, since_date: Optional[str] = None,
                    overlap_minutes: Optional[int] = None) -> Tuple[str, str]:
    """(UTC start time, description) of what this run should download from KoBo.

    Normally: everything since the last successful download, minus kobo_import.overlap_hours
    (submissions seen before are skipped by their _id). First run: the last lookback_days.
    --since: from that local date. Approval mode: never less than lookback_days, so
    late approvals are still picked up.
    """
    if since_date:
        return _local_midnight_utc(dt.date.fromisoformat(since_date)), f"since {since_date} (--since)"
    lookback = fetch_since(cfg, today)
    last = get_meta(conn, LAST_FETCH_KEY)
    if not last:
        return lookback, f"first download: last {cfg.lookback_days} days"
    overlap = overlap_minutes if overlap_minutes is not None else cfg.overlap_hours * 60
    start = dt.datetime.strptime(last, KOBO_TIME) - dt.timedelta(minutes=overlap)
    since = start.strftime(KOBO_TIME)
    last_local = (dt.datetime.strptime(last, KOBO_TIME).replace(tzinfo=dt.timezone.utc)
                  .astimezone().strftime("%Y-%m-%d %H:%M"))
    if cfg.required_validation and lookback < since:
        return lookback, f"last {cfg.lookback_days} days (approval required, for late approvals)"
    span = f"{overlap // 60} h" if overlap % 60 == 0 else f"{overlap} min"
    return since, f"since the last download ({last_local}) minus {span} overlap"
