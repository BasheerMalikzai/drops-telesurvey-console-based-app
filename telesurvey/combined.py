"""Combined export of the telesurvey and face-to-face answers in one table.

Telesurvey (TS): only submissions whose call status is "picked up" (code 1 by default).
Face-to-face (FF): every submission.
Both forms ask the same demographic questions and q1-q4 under the same names, so the rows
line up; a "source" column says where each row came from.

    python -m telesurvey export-combined            # codes, as stored in KoBo
    python -m telesurvey export-combined --labels   # choice labels instead of codes

Writes data/exports/combined_<date>.xlsx (if openpyxl is installed) and .csv.
Changes nothing in KoBo or the database.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import ConfigError
from .f2f import ALL_TIME
from .kobo import KoboClient, get_field

log = logging.getLogger(__name__)

# output column -> question name in both forms
DEMOGRAPHICS = [("Age Range", "age"), ("Children", "children"), ("Education", "education"),
                ("Gender", "gender"), ("income", "income"), ("Marital Status", "marital_status"),
                ("Profession Code", "profession"), ("Province Code", "province")]
QUESTIONS = ["q1", "q2", "q3", "q4"]


def _label(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


class FormInfo:
    """Question labels and choice labels of one form (from the KoBo asset)."""

    def __init__(self, asset: dict):
        content = asset.get("content", {})
        self.name = asset.get("name", "")
        self.labels: Dict[str, str] = {}
        self.lists: Dict[str, str] = {}
        for q in content.get("survey", []):
            n = q.get("name") or q.get("$autoname")
            if n:
                self.labels[n] = _label(q.get("label"))
                if q.get("select_from_list_name"):
                    self.lists[n] = q["select_from_list_name"]
        self.choices: Dict[Tuple[str, str], str] = {}
        for c in content.get("choices", []):
            code = c.get("name") or c.get("$autoname")
            if code is not None:
                self.choices[(c.get("list_name"), str(code))] = _label(c.get("label"))

    def choice(self, question: str, code: str) -> str:
        """The label of a select_one answer (or each part of a select_multiple)."""
        lst = self.lists.get(question)
        if not lst or code == "":
            return code
        return " ".join(self.choices.get((lst, part), part) for part in code.split())


def _client(cfg, uid: str) -> KoboClient:
    c = copy.copy(cfg)
    c.asset_uid = uid
    return KoboClient(c)


def _sort_key(rec) -> str:
    return str(rec.get("_submission_time", ""))


def build_rows(cfg, ts_records, ts_info: FormInfo, ff_records, ff_info: FormInfo,
               ts_status: List[str], labels: bool, ff_id_field: str) -> Tuple[List[str], List[list]]:
    q_headers = []
    for n, q in enumerate(QUESTIONS, 1):
        text = ts_info.labels.get(q) or ff_info.labels.get(q) or ""
        q_headers.append(text if text.lower().startswith(f"question {n}") else f"Question {n}: {text}".rstrip(": "))
    header = ["SN#"] + [h for h, _ in DEMOGRAPHICS] + q_headers + ["source", "idnumber"]

    def row(rec, info: FormInfo, source: str, who: str) -> list:
        vals = []
        for _, q in DEMOGRAPHICS + [(None, q) for q in QUESTIONS]:
            v = get_field(rec, q)
            v = " ".join(map(str, v)) if isinstance(v, list) else str(v if v is not None else "").strip()
            vals.append(info.choice(q, v) if labels else v)
        return vals + [source, who]

    status = {str(s).strip() for s in ts_status}
    out = []
    for rec in sorted(ts_records, key=_sort_key):
        if str(get_field(rec, cfg.field_status) or "").strip() in status:
            who = str(get_field(rec, cfg.field_surveyor) or "").strip()
            out.append(row(rec, ts_info, "TS", who))
    for rec in sorted(ff_records, key=_sort_key):
        who = str(get_field(rec, ff_id_field) or rec.get("_submitted_by") or "").strip()
        out.append(row(rec, ff_info, "FF", who))
    return header, [[i] + r for i, r in enumerate(out, 1)]


def write(header, rows, out_base: Path) -> List[Path]:
    out_base.parent.mkdir(parents=True, exist_ok=True)
    written = []
    csv_path = out_base.with_suffix(".csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: Excel shows Dari/Pashto right
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    written.append(csv_path)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        log.info("openpyxl not installed: wrote CSV only (.venv/bin/pip install openpyxl for Excel)")
        return written
    wb = Workbook()
    ws = wb.active
    ws.title = "Combined"
    ws.append(header)
    for r in rows:
        ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for i, h in enumerate(header, 1):
        width = min(max(len(str(h)), 8) + 2, 45)
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    xlsx_path = out_base.with_suffix(".xlsx")
    wb.save(xlsx_path)
    written.insert(0, xlsx_path)
    return written


def run(cfg, labels: bool = False, out: Optional[str] = None) -> int:
    raw = json.loads(cfg.config_path.read_text(encoding="utf-8")) if cfg.config_path else {}
    ff_sec = raw.get("face_to_face") or {}
    sec = raw.get("combined_export") or {}
    ff_uid = str(ff_sec.get("form_uid", "")).strip()
    if not cfg.asset_uid:
        raise ConfigError("Telesurvey form not set: KOBO_ASSET_UID in .env")
    if not ff_uid:
        raise ConfigError("Face-to-face form not set: face_to_face.form_uid in config.json")
    ts_status = [str(s) for s in sec.get("ts_call_status", ["1"])]
    ff_id_field = str(sec.get("ff_id_field") or ff_sec.get("surveyor_field") or "_submitted_by")

    ts, ff = _client(cfg, cfg.asset_uid), _client(cfg, ff_uid)
    ts_info, ff_info = FormInfo(ts.asset()), FormInfo(ff.asset())
    ts_records = ts.fetch_submissions(ALL_TIME)
    ff_records = ff.fetch_submissions(ALL_TIME)
    header, rows = build_rows(cfg, ts_records, ts_info, ff_records, ff_info, ts_status, labels, ff_id_field)

    n_ts = sum(1 for r in rows if r[-2] == "TS")
    base = Path(out) if out else cfg.output_dir.parent / "exports" / f"combined_{dt.date.today().isoformat()}"
    if base.suffix.lower() in (".csv", ".xlsx"):
        base = base.with_suffix("")
    files = write(header, rows, base)
    print(f"\n  Telesurvey ({ts_info.name}): {n_ts} of {len(ts_records)} submissions with call status "
          f"{', '.join(ts_status)}")
    print(f"  Face-to-face ({ff_info.name}): {len(rows) - n_ts} submissions")
    print(f"  {len(rows)} rows, {'labels' if labels else 'codes'}; written to:")
    for f in files:
        print(f"    {f}")
    return 0
