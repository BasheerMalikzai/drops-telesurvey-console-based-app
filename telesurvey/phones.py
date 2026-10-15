"""Reading the phone list into the master pool."""
from __future__ import annotations

import csv
import logging
from collections import Counter
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)


def norm_phone(value) -> str:
    """Digits only, so the number is a safe choice 'name' in KoBo and matches reliably."""
    return "".join(c for c in str(value or "") if c.isdigit())


def col_key(name) -> str:
    """'Phone Number', 'phone-number' and 'phone_number' all become 'phone_number'."""
    return "_".join(str(name or "").strip().lower().replace("-", " ").split())


def read_csv(path: Path, loose_names: bool = True) -> List[Dict[str, str]]:
    """Read a CSV saved with commas, semicolons or tabs (Excel uses ';' in some regions)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(65536)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample.split("\n", 1)[0] or sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        key = col_key if loose_names else (lambda k: str(k).strip().lower())
        return [{key(k): (v or "").strip() for k, v in row.items() if k is not None}
                for row in csv.DictReader(f, dialect=dialect)]


def _first_column(row: Dict[str, str], names) -> str:
    for name in names:
        if row.get(col_key(name)):
            return row[col_key(name)]
    return ""


def _language_of(cfg, row: Dict[str, str]) -> str:
    from .config import norm_language
    value = _first_column(row, cfg.language_columns)
    return norm_language(value) if value else ""


def import_phone_list(cfg, conn, today: str) -> int:
    """Add new numbers to phone_pool. For numbers already there, only a missing language is
    filled in (so a language column can be added to the phone list later)."""
    if not cfg.phone_list.exists():
        log.warning("Phone list %s not found; using the existing pool only", cfg.phone_list)
        return 0
    added = skipped = filled = 0
    langs = Counter()
    rows = read_csv(cfg.phone_list)
    if rows:
        columns = list(rows[0].keys())
        if not any(col_key(c) in columns for c in cfg.phone_columns):
            raise ValueError(
                f"{cfg.phone_list.name} has no phone-number column. Columns found: {', '.join(columns)}. "
                f"Rename the phone column to 'phone', or add its name to phone_list.phone_columns "
                f"in config.json")
        if not any(col_key(c) in columns for c in cfg.language_columns):
            log.warning("%s has no language column (looked for: %s); numbers can go to any caller",
                        cfg.phone_list.name, ", ".join(cfg.language_columns))
    for row in rows:
        phone = norm_phone(_first_column(row, cfg.phone_columns))
        if len(phone) < 7:
            skipped += 1
            continue
        language = _language_of(cfg, row)
        cur = conn.execute(
            "INSERT INTO phone_pool(phone, contact_name, added_on, language) VALUES(?,?,?,?) "
            "ON CONFLICT(phone) DO NOTHING",
            (phone, _first_column(row, cfg.name_columns), today, language or None))
        if cur.rowcount:
            added += 1
            langs[language or "no language"] += 1
        elif language:
            filled += conn.execute("UPDATE phone_pool SET language=? WHERE phone=? AND "
                                   "(language IS NULL OR language='')", (language, phone)).rowcount
    log.info("Phone list: %d new numbers added%s, %d invalid rows skipped", added,
             f" ({', '.join(f'{k}: {v}' for k, v in sorted(langs.items()))})" if added else "", skipped)
    if filled:
        log.info("Phone list: language filled in for %d numbers already in the pool", filled)
    return added
