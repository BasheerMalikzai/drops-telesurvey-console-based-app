"""Daily progress report for the face-to-face survey (a separate KoBo form).

Reads every submission of the face-to-face form (same KoBo account and token as the
telesurvey) and emails, per surveyor, the number of interviews on each day up to the report
day, and the total. Optionally broken down by a form field such as Province. It doesn't use the database or the phone pool, and changes nothing in KoBo.

Settings: the "face_to_face" section of config.json; the mail server comes from .env.
"""
from __future__ import annotations

import copy
import datetime as dt
import html
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import ConfigError
from .kobo import KoboClient, get_field
from .report import _kv_html, _kv_text, html_table, send_email, text_table

log = logging.getLogger(__name__)
ALL_TIME = "2000-01-01T00:00:00"


@dataclass
class F2FSurveyor:
    id: str
    name: str = ""
    active: bool = True


@dataclass
class F2FSettings:
    form_uid: str
    surveyor_field: str = "idnumber"
    surveyor_fallback: str = "_submitted_by"
    date_field: str = "_submission_time"
    breakdown_field: str = ""
    start_date: str = ""
    days_shown: int = 31                   # most recent days shown as columns
    report_day: str = "yesterday"          # or "today"
    subject_prefix: str = "[Face-to-face]"
    recipients: List[str] = field(default_factory=list)
    surveyors: List[F2FSurveyor] = field(default_factory=list)


def load_settings(cfg) -> F2FSettings:
    import json
    raw = json.loads(cfg.config_path.read_text(encoding="utf-8")) if cfg.config_path else {}
    sec = raw.get("face_to_face") or {}
    uid = (os.environ.get("KOBO_F2F_ASSET_UID") or str(sec.get("form_uid", ""))).strip()
    if not uid:
        raise ConfigError("Face-to-face form not set: add \"face_to_face\": {\"form_uid\": \"...\"} "
                          "to config.json (the UID is in the form's URL after /forms/)")
    people = []
    for i, s in enumerate(sec.get("surveyors", []), 1):
        sid = str(s.get("id", "")).strip()
        if not sid:
            raise ConfigError(f"face_to_face.surveyors #{i} has no 'id'")
        people.append(F2FSurveyor(id=sid, name=str(s.get("name", "")), active=bool(s.get("active", True))))
    report_day = str(sec.get("report_day", "yesterday"))
    if report_day not in ("yesterday", "today"):
        raise ConfigError("face_to_face.report_day must be \"yesterday\" or \"today\"")
    start = str(sec.get("start_date", "")).strip()
    if start:
        try:
            dt.date.fromisoformat(start)
        except ValueError:
            raise ConfigError(f"face_to_face.start_date must look like 2026-10-01, not {start!r}")
    return F2FSettings(
        form_uid=uid,
        surveyor_field=str(sec.get("surveyor_field", "idnumber")),
        surveyor_fallback=str(sec.get("surveyor_fallback", "_submitted_by")),
        date_field=str(sec.get("date_field", "_submission_time")),
        breakdown_field=str(sec.get("breakdown_field", "")),
        start_date=start,
        days_shown=max(1, int(sec.get("days_shown", 31) or 31)),
        report_day=report_day,
        subject_prefix=str(sec.get("subject_prefix", "[Face-to-face]")),
        recipients=[str(r).strip() for r in sec.get("recipients", []) if str(r).strip()],
        surveyors=people,
    )


# --------------------------------------------------------------------------- data

def local_day(rec, field_name: str) -> Optional[str]:
    """The local date of a submission. KoBo's _submission_time is UTC without an offset;
    form fields such as 'today' or 'start' are already local."""
    raw = get_field(rec, field_name)
    if not raw:
        return None
    text = str(raw).strip()
    if field_name.endswith("_submission_time"):
        try:
            t = dt.datetime.fromisoformat(text.replace("Z", "")[:26])
        except ValueError:
            return text[:10]
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone().date().isoformat()
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (t.astimezone() if t.tzinfo else t).date().isoformat()
    except ValueError:
        return text[:10]


def surveyor_of(rec, st: F2FSettings) -> str:
    v = get_field(rec, st.surveyor_field)
    if v in (None, "") and st.surveyor_fallback:
        v = get_field(rec, st.surveyor_fallback)
    return str(v or "").strip()


def fetch(cfg, st: F2FSettings) -> tuple:
    """(form name, submissions). Uses a copy of cfg pointed at the face-to-face form."""
    fcfg = copy.copy(cfg)
    fcfg.asset_uid = st.form_uid
    client = KoboClient(fcfg)
    name = client.asset().get("name", st.form_uid)
    since = ALL_TIME
    if st.start_date:
        # a day early: the API compares UTC times, the report uses local dates
        since = (dt.date.fromisoformat(st.start_date) - dt.timedelta(days=1)).isoformat() + "T00:00:00"
    return name, client.fetch_submissions(since)


@dataclass
class Progress:
    day: str
    form_name: str
    days: List[str]            # dates shown as columns, oldest first
    hidden_days: int           # earlier days not shown (still in the totals)
    rows: list                 # per surveyor: [label, count per day ..., total]
    totals: list
    breakdown: list            # [value, count per day ..., total]
    on_day: int                # interviews on the report day
    idle: List[str]            # active surveyors with nothing on the report day
    working: int               # active surveyors with at least one interview on the report day
    unknown: Dict[str, int]    # IDs in submissions that aren't in the list
    first_day: Optional[str]


def summarise(st: F2FSettings, form_name: str, records: list, day: str) -> Progress:
    per = Counter()            # (surveyor, date) -> interviews
    per_b = Counter()          # (breakdown value, date)
    seen = set()
    for rec in records:
        when = local_day(rec, st.date_field)
        if not when or (st.start_date and when < st.start_date) or when > day:
            continue
        seen.add(when)
        who = surveyor_of(rec, st) or "(no ID)"
        per[(who, when)] += 1
        if st.breakdown_field:
            per_b[(str(get_field(rec, st.breakdown_field) or "(blank)").strip(), when)] += 1

    first = st.start_date or (min(seen) if seen else day)
    d0, d1 = dt.date.fromisoformat(first), dt.date.fromisoformat(day)
    all_days = [(d0 + dt.timedelta(days=k)).isoformat() for k in range((d1 - d0).days + 1)] or [day]
    days = all_days[-st.days_shown:]
    hidden = len(all_days) - len(days)

    def line(label, key, counter):
        counts = [counter[(key, x)] for x in days]
        return [label] + counts + [sum(v for (k, _), v in counter.items() if k == key)]

    known = {s.id for s in st.surveyors}
    totals_by_who = Counter()
    for (who, _), v in per.items():
        totals_by_who[who] += v
    rows, idle, working = [], [], 0
    for s in st.surveyors:
        if not s.active and not totals_by_who[s.id]:
            continue
        rows.append(line(f"{s.id} {s.name}".strip() + ("" if s.active else " (inactive)"), s.id, per))
        if s.active:
            if per[(s.id, day)]:
                working += 1
            else:
                idle.append(f"{s.id} {s.name}".strip())
    unknown = {k: v for k, v in totals_by_who.items() if k not in known}
    for k in sorted(unknown, key=lambda k: -unknown[k]):
        rows.append(line(f"{k} (not in list)", k, per))
    totals = ["Total"] + [sum(r[1 + n] for r in rows) for n in range(len(days))] + [sum(r[-1] for r in rows)]

    values = sorted({k for (k, _) in per_b}, key=lambda k: (-sum(v for (b, _), v in per_b.items() if b == k), k))
    breakdown = [line(v, v, per_b) for v in values]
    return Progress(day=day, form_name=form_name, days=days, hidden_days=hidden, rows=rows, totals=totals,
                    breakdown=breakdown, on_day=per_total_on(per, day), idle=idle, working=working,
                    unknown=unknown, first_day=min(seen) if seen else None)


def per_total_on(per: Counter, day: str) -> int:
    return sum(v for (_, d), v in per.items() if d == day)


# --------------------------------------------------------------------------- email

def _short(d: str) -> str:
    return dt.date.fromisoformat(d).strftime("%d %b")


def build(st: F2FSettings, p: Progress) -> tuple:
    active = sum(1 for s in st.surveyors if s.active)
    subject = (f"{st.subject_prefix} Daily progress {p.day}: {p.on_day} interviews"
               + (f" ({p.working}/{active} surveyors)" if active else ""))
    since = st.start_date or p.first_day or "-"
    facts = [("Report date", p.day), ("Form", p.form_name),
             ("Interviews on the day", str(p.on_day)),
             ("Surveyors with interviews", f"{p.working} of {active}" if active else "-"),
             ("Interviews since " + since, str(p.totals[-1]))]
    cols = ["Surveyor"] + [_short(d) for d in p.days] + ["Total"]
    note = (f"Columns show the last {len(p.days)} days; the {p.hidden_days} earlier day(s) are "
            f"included in Total." if p.hidden_days else "")

    text = [f"Face-to-face survey: daily progress for {p.day}", "", _kv_text(facts), "",
            "Interviews per surveyor per day", text_table(cols, p.rows, p.totals)]
    h3 = '<h3 style="font:bold 15px Arial,sans-serif;margin:12px 0 4px">'
    body = [f'<h2 style="font:bold 18px Arial,sans-serif;margin:0 0 8px">Face-to-face survey: '
            f"daily progress for {html.escape(p.day)}</h2>", _kv_html(facts),
            f"{h3}Interviews per surveyor per day</h3>", html_table(cols, p.rows, p.totals)]
    if note:
        text.append(note)
        body.append(f'<p style="font:12px Arial,sans-serif;color:#57606a">{html.escape(note)}</p>')
    if p.idle:
        msg = "No interviews on " + p.day + ": " + ", ".join(p.idle)
        text += ["", msg]
        body.append(f'<p style="font:14px Arial,sans-serif;color:#9a6700">{html.escape(msg)}</p>')
    if p.unknown:
        msg = ("Submissions from IDs that aren't in face_to_face.surveyors (check the ID entered, "
               "or add them to config.json): " + ", ".join(f"{k} ({v})" for k, v in p.unknown.items()))
        text += ["", msg]
        body.append(f'<p style="font:14px Arial,sans-serif;color:#57606a">{html.escape(msg)}</p>')
    if p.breakdown:
        bcols = [st.breakdown_field] + [_short(d) for d in p.days] + ["Total"]
        btot = ["Total"] + [sum(r[1 + n] for r in p.breakdown) for n in range(len(p.days))] \
            + [sum(r[-1] for r in p.breakdown)]
        text += ["", f"By {st.breakdown_field}", text_table(bcols, p.breakdown, btot)]
        body += [f"{h3}By {html.escape(st.breakdown_field)}</h3>", html_table(bcols, p.breakdown, btot)]
    text += ["", "Counts every submission, whatever its validation status. Dates are in local time."]
    body.append('<p style="font:12px Arial,sans-serif;color:#57606a">Counts every submission, whatever '
                'its validation status. Dates are in local time.</p>')
    return subject, "\n".join(text) + "\n", "<div>" + "".join(body) + "</div>"


def report_day_for(st: F2FSettings, run_day: str) -> str:
    d = dt.date.fromisoformat(run_day)
    return (d - dt.timedelta(days=1)).isoformat() if st.report_day == "yesterday" else run_day


def run(cfg, run_day: str, day: Optional[str] = None, send: bool = True) -> int:
    """Build the report and email it (or print it with send=False). On failure, emails the error."""
    try:
        st = load_settings(cfg)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    day = day or report_day_for(st, run_day)
    to = st.recipients or cfg.report_recipients
    try:
        form_name, records = fetch(cfg, st)
        p = summarise(st, form_name, records, day)
        subject, text, html_body = build(st, p)
    except Exception as e:
        log.exception("Face-to-face report failed")
        if send and to and cfg.smtp.get("host"):
            try:
                send_email(cfg, f"{st.subject_prefix} FAILED: daily progress {day}",
                           f"The face-to-face progress report for {day} could not be made:\n\n{e}\n\n"
                           f"Check the form UID in config.json (face_to_face.form_uid) and run\n"
                           f"  .venv/bin/python -m telesurvey f2f-report --print\n", recipients=to)
            except Exception as mail_error:
                log.error("Could not email the failure either: %s", mail_error)
        return 1
    if not send:
        print(text)
        return 0
    try:
        send_email(cfg, subject, text, html_body, recipients=to)
    except Exception as e:
        log.error("Face-to-face report not sent: %s", e)
        return 1
    log.info("Face-to-face report for %s sent to %s (%d interviews on the day)", day, ", ".join(to), p.on_day)
    return 0
