"""Reading the phone list into the master pool."""
from __future__ import annotations

import csv
import logging
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


def import_phone_list(cfg, conn, today: str) -> int:
    if not cfg.phone_list.exists():
        log.warning("Phone list %s not found; using the existing pool only", cfg.phone_list)
        return 0
    added = skipped = 0
    for row in read_csv(cfg.phone_list):
        phone = norm_phone(row.get("phone") or row.get("phone_number"))
        if len(phone) < 7:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT INTO phone_pool(phone, contact_name, added_on) VALUES(?,?,?) "
            "ON CONFLICT(phone) DO NOTHING",
            (phone, row.get("name", ""), today))
        added += cur.rowcount
    log.info("Phone list: %d new numbers added, %d invalid rows skipped", added, skipped)
    return added
