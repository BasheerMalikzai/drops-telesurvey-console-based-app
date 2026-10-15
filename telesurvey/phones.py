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


def read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(f)]


def _language_of(cfg, row: Dict[str, str]) -> str:
    from .config import norm_language
    for col in cfg.language_columns:
        if row.get(col):
            return norm_language(row[col])
    return ""


def import_phone_list(cfg, conn, today: str) -> int:
    """Add new numbers to phone_pool. For numbers already there, only a missing language is
    filled in (so a language column can be added to the phone list later)."""
    if not cfg.phone_list.exists():
        log.warning("Phone list %s not found; using the existing pool only", cfg.phone_list)
        return 0
    added = skipped = filled = 0
    langs = Counter()
    for row in read_csv(cfg.phone_list):
        phone = norm_phone(row.get("phone") or row.get("phone_number"))
        if len(phone) < 7:
            skipped += 1
            continue
        language = _language_of(cfg, row)
        cur = conn.execute(
            "INSERT INTO phone_pool(phone, contact_name, added_on, language) VALUES(?,?,?,?) "
            "ON CONFLICT(phone) DO NOTHING",
            (phone, row.get("name", ""), today, language or None))
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
