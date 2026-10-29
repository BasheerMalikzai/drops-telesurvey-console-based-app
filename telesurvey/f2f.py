"""Daily progress report for the face-to-face survey (a separate KoBo form).

Reads every submission of the face-to-face form (same KoBo account and token as the
telesurvey) and emails, per province and per surveyor (the province set for each surveyor in
config.json): surveys on the last day (the report day) and in total. Optionally a
day-by-day table (days_shown) and a breakdown by a form field. It doesn't use the database or the phone pool, and changes nothing in KoBo.

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
    province: str = ""
    active: bool = True


@dataclass
class F2FSettings:
    form_uid: str
    surveyor_field: str = "idnumber"
    surveyor_fallback: str = "_submitted_by"
    date_field: str = "_submission_time"
    breakdown_field: str = ""
    start_date: str = ""
    days_shown: int = 0                    # optional day-by-day table: most recent days (0 = none)
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
        people.append(F2FSurveyor(id=sid, name=str(s.get("name", "")), active=bool(s.get("active", True)),
                                  province=str(s.get("province", "")).strip()))
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
        days_shown=max(0, int(sec.get("days_shown", 0))),
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
    surveyors: list            # [surveyor, province, report day, total]
    s_total: list
    provinces: list            # [province, report day, total]
    p_total: list
    days: List[str]            # day-by-day table: dates shown, oldest first
    hidden_days: int
    daily: list                # [surveyor, count per day ..., total]
    d_total: list
    breakdown: list            # [form-field value, report day, total]
    on_day: int
    all_time: int
    idle: List[str]
    working: int
    unknown: Dict[str, int]
    first_day: Optional[str]


def summarise(st: F2FSettings, form_name: str, records: list, day: str) -> Progress:
    per = Counter()            # (surveyor, date) -> interviews
    per_b = Counter()          # (form-field value, date)
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

    total_of, day_of = Counter(), Counter()
    for (who, when), v in per.items():
        total_of[who] += v
        if when == day:
            day_of[who] += v

    known = {s.id: s for s in st.surveyors}
    unknown = {k: v for k, v in total_of.items() if k not in known}
    # who appears in the report: active surveyors, inactive ones with interviews, unknown IDs
    people = [(s.id, f"{s.name or s.id}" + ("" if s.active else " (inactive)"), s.province or "(not set)", s.active)
              for s in st.surveyors if s.active or total_of[s.id]]
    people += [(k, f"{k} (not in list)", "(unknown)", False) for k in sorted(unknown, key=lambda k: -unknown[k])]

    # 1) per surveyor
    srows = [[label, prov, day_of[sid], total_of[sid]] for sid, label, prov, _ in people]
    s_total = ["Total", "", sum(r[2] for r in srows), sum(r[3] for r in srows)]
    idle = [f"{known[sid].name or sid} ({known[sid].province})" if known[sid].province else (known[sid].name or sid)
            for sid, _, _, active in people if active and not day_of[sid]]
    working = sum(1 for sid, _, _, active in people if active and day_of[sid])

    # 2) per province (from each surveyor's province in config.json)
    pv = {}
    for sid, _, prov, active in people:
        r = pv.setdefault(prov, [prov, 0, 0, 0, 0])
        r[1] += 1 if active else 0
        r[2] += 1 if active and day_of[sid] else 0
        r[3] += day_of[sid]
        r[4] += total_of[sid]
    prows = sorted(pv.values(), key=lambda r: (r[0].startswith("("), r[0].lower()))
    prows = [[r[0], r[3], r[4]] for r in prows]
    p_total = ["Total", sum(r[1] for r in prows), sum(r[2] for r in prows)]

    # 3) day by day per surveyor
    days, hidden, daily, d_total = [], 0, [], []
    if st.days_shown:
        first = st.start_date or (min(seen) if seen else day)
        d0, d1 = dt.date.fromisoformat(first), dt.date.fromisoformat(day)
        all_days = [(d0 + dt.timedelta(days=k)).isoformat() for k in range((d1 - d0).days + 1)] or [day]
        days = all_days[-st.days_shown:]
        hidden = len(all_days) - len(days)
        daily = [[label] + [per[(sid, x)] for x in days] + [total_of[sid]] for sid, label, _, _ in people]
        d_total = ["Total"] + [sum(r[1 + n] for r in daily) for n in range(len(days))] + [sum(r[-1] for r in daily)]

    # 4) optional: by a form field
    b_day, b_tot = Counter(), Counter()
    for (b, when), v in per_b.items():
        b_tot[b] += v
        if when == day:
            b_day[b] += v
    breakdown = [[b, b_day[b], b_tot[b]] for b in sorted(b_tot, key=lambda b: (-b_tot[b], b))]

    return Progress(day=day, form_name=form_name, surveyors=srows, s_total=s_total, provinces=prows,
                    p_total=p_total, days=days, hidden_days=hidden, daily=daily, d_total=d_total,
                    breakdown=breakdown, on_day=s_total[2], all_time=s_total[3], idle=idle, working=working,
                    unknown=unknown, first_day=min(seen) if seen else None)


# --------------------------------------------------------------------------- email

def _short(d: str) -> str:
    return dt.date.fromisoformat(d).strftime("%d %b")


def build(st: F2FSettings, p: Progress) -> tuple:
    active = sum(1 for s in st.surveyors if s.active)
    subject = (f"{st.subject_prefix} Daily progress {p.day}: {p.on_day} surveys"
               + (f" ({p.working}/{active} surveyors)" if active else ""))
    since = st.start_date or p.first_day or "-"
    last = _short(p.day)
    facts = [("Report date", p.day), ("Form", p.form_name),
             ("Surveys on the last day", str(p.on_day)),
             ("Surveyors who surveyed on the last day", f"{p.working} of {active}" if active else "-"),
             ("Total surveys since " + since, str(p.all_time))]
    h3 = '<h3 style="font:bold 15px Arial,sans-serif;margin:16px 0 4px">'
    text = [f"Face-to-face survey: daily progress for {p.day}", "", _kv_text(facts)]
    body = [f'<h2 style="font:bold 18px Arial,sans-serif;margin:0 0 8px">Face-to-face survey: '
            f"daily progress for {html.escape(p.day)}</h2>", _kv_html(facts)]

    def section(title, cols, rows, total):
        text.extend(["", title, text_table(cols, rows, total)])
        body.extend([f"{h3}{html.escape(title)}</h3>", html_table(cols, rows, total)])

    def para(msg, color="#57606a", size=14):
        text.extend(["", msg])
        body.append(f'<p style="font:{size}px Arial,sans-serif;color:{color}">{html.escape(msg)}</p>')

    last_col = f"Last day ({last})"
    total_col = f"Total since {_short(since)}" if since != "-" else "Total"
    section("Per province", ["Province", last_col, total_col], p.provinces, p.p_total)
    section("Per surveyor", ["Surveyor", "Province", last_col, total_col], p.surveyors, p.s_total)
    if p.idle:
        para(f"No surveys on {p.day}: " + ", ".join(p.idle), color="#9a6700")
    if p.unknown:
        para("Submissions from accounts/IDs that aren't in face_to_face.surveyors (check who sent them, "
             "or add them to config.json): " + ", ".join(f"{k} ({v})" for k, v in p.unknown.items()))
    if p.daily:
        section("Day by day, per surveyor", ["Surveyor"] + [_short(d) for d in p.days] + ["Total"],
                p.daily, p.d_total)
        if p.hidden_days:
            para(f"Columns show the last {len(p.days)} days; the {p.hidden_days} earlier day(s) are included "
                 f"in Total.", size=12)
    if p.breakdown:
        section(f"By {st.breakdown_field} (from the form)", [st.breakdown_field, last, "Total"], p.breakdown,
                ["Total", sum(r[1] for r in p.breakdown), sum(r[2] for r in p.breakdown)])
    para("Counts every submission, whatever its validation status. Dates are in local time.", size=12)
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
