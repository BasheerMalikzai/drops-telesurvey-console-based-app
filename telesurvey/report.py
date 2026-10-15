"""Email report sent after every daily run (success or failure)."""
from __future__ import annotations

import datetime as dt
import html
import logging
import smtplib
import ssl
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Optional

from .results import classify

log = logging.getLogger(__name__)

# Always shown, in this order (the four the team asked for); other categories appear when used
MAIN_CATEGORIES = ["successful", "rescheduled", "declined", "no_pickup"]


@dataclass
class RunInfo:
    """Collected during a daily run and used for the report."""
    day: str
    started: dt.datetime = field(default_factory=dt.datetime.now)
    status: str = "running"            # ok | failed | upload_failed
    step: str = "starting"
    error: str = ""
    trace: str = ""
    db_changed: bool = False
    imported: int = 0
    fetched: int = 0
    window: str = ""
    counts: Counter = field(default_factory=Counter)
    expired: int = 0
    pulled: int = 0
    assigned: int = 0
    reused: bool = False
    upload: str = "not attempted"
    options: List[str] = field(default_factory=list)

    def fail(self, exc: BaseException) -> None:
        import re
        self.status = "failed"
        msg = re.sub(r" for url: \S+", "", str(exc))          # drop long request URLs
        self.error = msg if type(exc).__name__ == "DatabaseError" else f"{type(exc).__name__}: {msg}"
        self.trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]


# --------------------------------------------------------------------------- data

def caller_table(cfg, conn, report_date: str):
    """Rows of per-caller counts for submissions made on report_date (local time)."""
    rows = conn.execute("SELECT surveyor_id, outcome, validation FROM submissions WHERE submit_date=?",
                        (report_date,)).fetchall()
    stats = defaultdict(Counter)
    for r in rows:
        who = r["surveyor_id"] or "(no caller ID)"
        c = stats[who]
        c["submitted"] += 1
        state = classify(cfg, r["validation"] or "")
        if r["validation"] == "expired":
            state = "awaiting"
        if state == "approved":
            c["approved"] += 1
            c[cfg.category_of(r["outcome"])] += 1
        elif state == "rejected":
            c["rejected"] += 1
        else:
            c["awaiting"] += 1

    used = Counter()
    for c in stats.values():
        used.update(c)
    extra = [n for n in cfg.categories if n not in MAIN_CATEGORIES and used[n]]
    cats = [n for n in MAIN_CATEGORIES if n in cfg.categories] + extra + (["other"] if used["other"] else [])
    labels = {n: cfg.categories[n]["label"] if n in cfg.categories else "Other" for n in cats}
    if cfg.required_validation:
        cols = ["Caller", "Submitted", "Approved"] + [labels[n] for n in cats] + ["Awaiting review", "Rejected"]
        keys = ["submitted", "approved"] + cats + ["awaiting", "rejected"]
    else:  # every submission counts: no approval columns
        cols = ["Caller", "Submitted"] + [labels[n] for n in cats]
        keys = ["submitted"] + cats

    def sort_key(sid):
        return (0, int(sid)) if sid.isdigit() else (1, sid)

    body = []
    for sid in sorted(stats, key=sort_key):
        name = cfg.surveyor_name(sid)
        body.append([f"{sid} {name}".strip()] + [stats[sid][k] for k in keys])
    total = ["Total"] + [sum(stats[s][k] for s in stats) for k in keys]
    return cols, body, total


def pool_lines(cfg, conn) -> list:
    from .export import call_list_summary, pool_summary
    p = pool_summary(conn)
    s = call_list_summary(cfg, conn)
    from .export import language_summary
    lines = [("phone_pool", f"{p['total']} numbers, {p['not_pulled']} not pulled yet")]
    lines += [(f"  {lang}", f"{total} numbers, {left} not pulled yet") for lang, total, left in language_summary(conn)]
    lines += [(f"call_list: {k}", v) for k, v in s["status"]]
    lines += [(f"  last result: {k}", v) for k, v in s["result"]]
    return lines


def assignment_counts(cfg, conn, day: str) -> List[tuple]:
    rows = conn.execute("""SELECT surveyor_id, COUNT(*) AS c FROM assignments WHERE assign_date=?
                           GROUP BY surveyor_id ORDER BY surveyor_id""", (day,)).fetchall()
    speaks = {s.id: "/".join(s.languages) for s in cfg.surveyors}
    return [(f"{r['surveyor_id']} {cfg.surveyor_name(r['surveyor_id'])}".strip()
             + (f" ({speaks[r['surveyor_id']]})" if speaks.get(r["surveyor_id"]) else ""), r["c"]) for r in rows]


def last_log_lines(cfg, n: int = 25) -> str:
    try:
        lines = cfg.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(log file not readable)"


# --------------------------------------------------------------------------- formatting

def text_table(cols, body, total) -> str:
    grid = [cols] + [[str(x) for x in r] for r in body] + [[str(x) for x in total]]
    widths = [max(len(r[i]) for r in grid) for i in range(len(cols))]
    fmt = lambda r: "  ".join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(r))
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(grid[0]), sep] + [fmt(r) for r in grid[1:-1]] + [sep, fmt(grid[-1])])


def html_table(cols, body, total) -> str:
    th = "".join(f'<th style="{_cell(i, head=True)}">{html.escape(c)}</th>' for i, c in enumerate(cols))
    trs = []
    for r in body:
        trs.append("<tr>" + "".join(f'<td style="{_cell(i)}">{html.escape(str(v))}</td>'
                                    for i, v in enumerate(r)) + "</tr>")
    trs.append("<tr>" + "".join(f'<td style="{_cell(i, total=True)}">{html.escape(str(v))}</td>'
                                for i, v in enumerate(total)) + "</tr>")
    return (f'<table style="border-collapse:collapse;font:14px Arial,sans-serif;margin:8px 0 16px">'
            f"<tr>{th}</tr>{''.join(trs)}</table>")


def _cell(i, head=False, total=False) -> str:
    s = "padding:6px 10px;border:1px solid #d0d7de;"
    s += "text-align:left;" if i == 0 else "text-align:right;"
    if head:
        s += "background:#f3f5f7;font-weight:bold;"
    if total:
        s += "font-weight:bold;background:#fafbfc;"
    return s


def _kv_text(pairs) -> str:
    w = max(len(k) for k, _ in pairs)
    return "\n".join(f"  {k.ljust(w)}  {v}" for k, v in pairs)


def _kv_html(pairs) -> str:
    rows = "".join(f'<tr><td style="padding:3px 12px 3px 0;color:#57606a">{html.escape(k)}</td>'
                   f'<td style="padding:3px 0">{html.escape(str(v))}</td></tr>' for k, v in pairs)
    return f'<table style="font:14px Arial,sans-serif;margin:4px 0 16px">{rows}</table>'


# --------------------------------------------------------------------------- report

def build(cfg, conn, info: Optional[RunInfo], report_date: str, run_day: str):
    """Returns (subject, text, html). conn may be None if the database was unreachable."""
    p = cfg.report_subject_prefix
    failed = info is not None and info.status in ("failed", "upload_failed")
    when = (info.started if info else dt.datetime.now()).strftime("%Y-%m-%d %H:%M")

    if info is None:
        subject = f"{p} Call report for {report_date}"
        headline = f"Call report for {report_date} (sent manually {when})"
    elif info.status == "failed":
        subject = f"{p} FAILED: daily run {run_day}"
        headline = f"The daily run on {when} FAILED while {info.step}."
    elif info.status == "upload_failed":
        subject = f"{p} Upload FAILED: daily run {run_day}"
        headline = (f"The daily run on {when} drew today's assignment, but uploading it to KoBo FAILED. "
                    f"Surveyors may still see yesterday's list.")
    else:
        subject = f"{p} Daily run OK {run_day}: {info.assigned} numbers assigned"
        headline = f"The daily run on {when} completed successfully."

    text, parts = [headline, ""], [f'<p style="font:15px Arial,sans-serif;'
                                   f'{"color:#cf222e;font-weight:bold" if failed else ""}">'
                                   f"{html.escape(headline)}</p>"]

    if failed:
        what_now = ("Nothing was changed in the database; the run can simply be repeated: ./daily.sh"
                    if info.status == "failed" else
                    "The assignment is saved. Retry the upload with ./daily.sh (it reuses today's draw), "
                    "then check with: .venv/bin/python verify_kobo_media.py")
        pairs = [("Failed step", info.step), ("Error", info.error or "(see log)"), ("What to do", what_now)]
        text += ["What went wrong", _kv_text(pairs), ""]
        parts.append("<h3 style='font:bold 15px Arial'>What went wrong</h3>" + _kv_html(pairs))

    if conn is not None:
        cols, body, total = caller_table(cfg, conn, report_date)
        title = f"Calls submitted on {report_date}, per caller"
        note = ("Result columns count approved submissions only."
                if cfg.required_validation else "All submissions are counted, whatever their validation status.")
        if body:
            text += [title, text_table(cols, body, total), note, ""]
            parts.append(f"<h3 style='font:bold 15px Arial'>{html.escape(title)}</h3>"
                         + html_table(cols, body, total)
                         + f"<p style='font:13px Arial;color:#57606a'>{html.escape(note)}</p>")
        else:
            text += [title, "  No submissions were made on this day.", ""]
            parts.append(f"<h3 style='font:bold 15px Arial'>{html.escape(title)}</h3>"
                         "<p style='font:14px Arial'>No submissions were made on this day.</p>")

    if info is not None and not (failed and info.status == "failed"):
        c = info.counts
        pairs = [("New numbers imported", info.imported),
                 ("Submissions checked", f"{info.fetched} ({info.window})" if info.window else info.fetched)]
        if cfg.required_validation:
            pairs += [("Results applied (approved)", c["applied"]),
                      ("Numbers held, awaiting review", c["held"]),
                      ("Released after rejection", c["released"])]
        else:
            pairs += [("New results applied", c["applied"])]
        pairs += [("New numbers pulled into call_list", info.pulled),
                 ("Numbers assigned today", f"{info.assigned}" + (" (existing draw reused)" if info.reused else "")),
                 ("Upload to KoBo", info.upload)]
        if info.expired:
            pairs.insert(len(pairs) - 2, ("Released, never reviewed", info.expired))
        if info.options:
            pairs.append(("Options", " ".join(info.options)))
        text += ["This run", _kv_text(pairs), ""]
        parts.append("<h3 style='font:bold 15px Arial'>This run</h3>" + _kv_html(pairs))

    if conn is not None:
        assigned = assignment_counts(cfg, conn, run_day)
        if assigned:
            text += [f"Today's assignment ({run_day})", _kv_text(assigned), ""]
            parts.append(f"<h3 style='font:bold 15px Arial'>Today's assignment ({html.escape(run_day)})</h3>"
                         + _kv_html(assigned))
        pp = pool_lines(cfg, conn)
        text += ["Numbers", _kv_text(pp), ""]
        parts.append("<h3 style='font:bold 15px Arial'>Numbers</h3>" + _kv_html(pp))

    if failed:
        logs = last_log_lines(cfg)
        text += ["Last log lines", logs, ""]
        parts.append("<h3 style='font:bold 15px Arial'>Last log lines</h3>"
                     f"<pre style='font:12px monospace;background:#f6f8fa;padding:8px'>{html.escape(logs)}</pre>")
        if info.trace:
            text += ["Technical details", info.trace]
            parts.append("<h3 style='font:bold 15px Arial'>Technical details</h3>"
                         f"<pre style='font:12px monospace;background:#f6f8fa;padding:8px'>{html.escape(info.trace)}</pre>")

    footer = f"Sent by the telesurvey tool ({cfg.db_description})."
    text.append(footer)
    parts.append(f"<p style='font:12px Arial;color:#8c959f'>{html.escape(footer)}</p>")
    return subject, "\n".join(text), "<html><body>" + "".join(parts) + "</body></html>"


# --------------------------------------------------------------------------- sending

def send_email(cfg, subject: str, text: str, html_body: Optional[str] = None,
               recipients: Optional[List[str]] = None) -> None:
    """Raises on failure; callers decide whether that matters."""
    smtp = cfg.smtp
    to = recipients or cfg.report_recipients
    if not to:
        raise RuntimeError("No recipients: add addresses to report.recipients in config.json")
    if not smtp.get("host"):
        raise RuntimeError("No mail server: set SMTP_HOST (and SMTP_USER/SMTP_PASSWORD) in .env")
    sender = smtp.get("sender") or smtp.get("user")
    if not sender:
        raise RuntimeError("No sender address: set SMTP_FROM in .env")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    security = smtp.get("security", "starttls")
    port = int(smtp.get("port") or {"ssl": 465, "starttls": 587}.get(security, 25))
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(smtp["host"], port, timeout=30)
    try:
        if security == "starttls":
            server.starttls(context=context)
        if smtp.get("user"):
            server.login(smtp["user"], smtp.get("password", ""))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def report_date_for(cfg, run_day: str) -> str:
    d = dt.date.fromisoformat(run_day)
    return (d - dt.timedelta(days=1)).isoformat() if cfg.report_day == "yesterday" else d.isoformat()


def send_run_report(cfg, info: RunInfo) -> None:
    """Called after every run. Never raises: a broken mail setup must not break the run."""
    if not cfg.report_enabled or cfg.report_send_on == "never":
        return
    if cfg.report_send_on == "failure" and info.status == "ok":
        return
    if not cfg.email_ready:
        log.info("Email report not sent: set report.recipients in config.json and SMTP_HOST in .env")
        return
    from . import db
    conn = None
    try:
        conn = db.connect(cfg, create_schema=False)
    except Exception as e:
        log.warning("Report will not include statistics (database unavailable: %s)", e)
    try:
        subject, text, html_body = build(cfg, conn, info, report_date_for(cfg, info.day), info.day)
        send_email(cfg, subject, text, html_body)
        log.info("Email report sent to %s", ", ".join(cfg.report_recipients))
    except Exception as e:
        log.error("Could not send the email report: %s", e)
    finally:
        if conn is not None:
            conn.close()
