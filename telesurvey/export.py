"""Writing the daily assignment file and printing status."""
from __future__ import annotations

import csv
import logging
from collections import Counter
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

COLUMNS = ["name", "label", "surveyor_id", "contact_name", "attempt_no", "previous_outcome", "assign_date",
           "language",
           # the same values under the phone list's names, for forms that use
           # search('Assignment', 'matches', 'assigned_to', ${idnumber}) and the like
           "assigned_to", "resp_phone_number", "resp_name"]


_QUERY = """SELECT a.phone, a.surveyor_id, a.called, p.contact_name, p.language,
                    c.attempts, c.last_result_code, c.status
             FROM assignments a
             JOIN call_list c ON c.phone = a.phone
             LEFT JOIN phone_pool p ON p.phone = a.phone
             WHERE a.assign_date=? ORDER BY a.surveyor_id, a.phone"""


def _write(path: Path, rows, today: str) -> None:
    """Write atomically, so KoBo is never sent a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)  # 'name' and 'label' are what select_one_from_file needs
        for r in rows:
            label = f"{r['phone']} - {r['contact_name']}" if r["contact_name"] else r["phone"]
            # attempts was already increased if a result came in today, so use the stored count
            attempt = r["attempts"] + (0 if r["called"] else 1)
            w.writerow([r["phone"], label, r["surveyor_id"], r["contact_name"] or "",
                        attempt, r["last_result_code"] or "", today, r["language"] or "",
                        r["surveyor_id"], r["phone"], r["contact_name"] or ""])
    tmp.replace(path)


def export_csv(cfg, conn, today: str) -> Optional[Path]:
    """Full day's assignment in assignments_<date>.csv; the KoBo media file only holds
    numbers that have no result yet (a sync may already have recorded some)."""
    rows = conn.execute(_QUERY, (today,)).fetchall()
    if not rows:
        return None
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    dated = cfg.output_dir / f"assignments_{today}.csv"
    _write(dated, rows, today)
    per = Counter(r["surveyor_id"] for r in rows)
    log.info("Wrote %s (per surveyor: %s)", dated, dict(sorted(per.items())))
    return export_remaining(cfg, conn, today, rows)


def export_remaining(cfg, conn, today: str, rows=None, quiet: bool = False) -> Optional[Path]:
    """Write the KoBo media file with only the numbers still to call today."""
    rows = rows if rows is not None else conn.execute(_QUERY, (today,)).fetchall()
    if not rows:
        return None
    # still to call: not called today, and not finished / held / removed meanwhile
    remaining = [r for r in rows if not r["called"] and r["status"] == "open"]
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    media = cfg.output_dir / cfg.media_filename
    _write(media, remaining, today)
    (log.debug if quiet else log.info)("Upload list: %d of %d numbers still to call", len(remaining), len(rows))
    return media


def print_status(cfg, conn, today: str) -> None:
    names = {s.id: s.name for s in cfg.surveyors}
    import datetime as _dt
    from .db import get_meta
    print(f"\nDatabase: {cfg.db_description}")
    from .sync import UPLOADED_AT_KEY, load_state
    last = get_meta(conn, UPLOADED_AT_KEY)
    every = (f"every {cfg.sync_every_minutes} min, {cfg.sync_from}-{cfg.sync_until}"
             if cfg.sync_every_minutes else "off")
    st = load_state(cfg)
    print(f"Sync: {every}; last sync: {st.get('last_success', 'never')}"
          + ("  ** FAILING: " + st.get("last_error", "") if st.get("failing") else ""))
    print(f"Last upload to KoBo: "
          f"{_dt.datetime.fromtimestamp(int(last)).strftime('%Y-%m-%d %H:%M') if last else 'never'}")
    pool = pool_summary(conn)
    print(f"\nphone_pool: {pool['total']} numbers ({pool['pulled']} pulled, {pool['not_pulled']} not pulled yet)")
    for lang, total, left in language_summary(conn):
        print(f"  {lang:<12} {total} numbers, {left} not pulled yet")
    print("\ncall_list by status:")
    for k, v in call_list_summary(cfg, conn)["status"]:
        print(f"  {k:<12} {v}")
    print("call_list by last result:")
    for k, v in call_list_summary(cfg, conn)["result"]:
        print(f"  {k:<16} {v}")

    print(f"\nToday's assignment ({today}):")
    rows = conn.execute("""SELECT surveyor_id, COUNT(*) AS c, SUM(called) AS d
                           FROM assignments WHERE assign_date=? GROUP BY surveyor_id
                           ORDER BY surveyor_id""", (today,)).fetchall()
    speaks = {s.id: "/".join(s.languages) or "any language" for s in cfg.surveyors}
    for r in rows:
        who = f"{r['surveyor_id']} {names.get(r['surveyor_id'], '')}".strip()
        print(f"  {who:<20} {r['c']} assigned, {r['d'] or 0} called  ({speaks.get(r['surveyor_id'], '?')})")
    if not rows:
        print("  (none yet)")


def pool_summary(conn) -> dict:
    total = conn.execute("SELECT COUNT(*) AS c FROM phone_pool").fetchone()["c"]
    pulled = conn.execute("SELECT COUNT(*) AS c FROM call_list").fetchone()["c"]
    return {"total": total, "pulled": pulled, "not_pulled": max(total - pulled, 0)}


def call_list_summary(cfg, conn) -> dict:
    """Counts by status, and by last result (with readable labels)."""
    status = [(r["status"], r["c"]) for r in conn.execute(
        "SELECT status, COUNT(*) AS c FROM call_list GROUP BY status ORDER BY status").fetchall()]
    result = []
    for r in conn.execute("""SELECT COALESCE(last_result, '') AS k, COUNT(*) AS c FROM call_list
                             GROUP BY COALESCE(last_result, '') ORDER BY 2 DESC""").fetchall():
        k = r["k"]
        label = "not called yet" if not k else (cfg.categories[k]["label"] if k in cfg.categories else k.capitalize())
        result.append((label, r["c"]))
    return {"status": status, "result": result}


def language_summary(conn):
    """[(language, numbers in phone_pool, of which not pulled yet)], or [] if no languages recorded."""
    rows = conn.execute("""SELECT COALESCE(NULLIF(p.language, ''), 'no language') AS lang,
                                  COUNT(*) AS total,
                                  SUM(CASE WHEN c.phone IS NULL THEN 1 ELSE 0 END) AS left_
                           FROM phone_pool p LEFT JOIN call_list c ON c.phone = p.phone
                           GROUP BY COALESCE(NULLIF(p.language, ''), 'no language')
                           ORDER BY 1""").fetchall()
    out = [(r["lang"], r["total"], r["left_"] or 0) for r in rows]
    return [] if [o[0] for o in out] in ([], ["no language"]) else out
