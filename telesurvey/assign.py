"""Choosing today's numbers and spreading them across callers.

    phone_pool ──pull──► call_list ──draw──► assignments (today only)

1. Candidates come from call_list: status 'open' whose callback date (if any) has come.
   Order: callbacks due, then other retries, then pulled numbers not called yet,
   random within each group.
2. If they can't fill today's slots, new numbers are pulled at random from phone_pool
   (numbers not yet in call_list): just the shortfall, or pull.batch_size at a time.
3. Rescheduled numbers go back to the caller who booked them; everything else is
   shuffled and given to the least-loaded caller.
4. assignments is emptied and refilled with today's draw. phone_pool is never changed.
"""
from __future__ import annotations

import logging
import random
from typing import List, Tuple

from .config import ConfigError
from .db import OPEN

log = logging.getLogger(__name__)


def _kind(r) -> str:
    if r["callback_date"]:
        return "callbacks"
    return "retries" if (r["attempts"] or r["last_result"]) else "never called"


def pull_numbers(cfg, conn, how_many: int, today: str) -> List[str]:
    """Move up to how_many random, not-yet-pulled numbers from phone_pool into call_list."""
    if how_many <= 0:
        return []
    rows = conn.execute("""SELECT p.phone FROM phone_pool p
                           LEFT JOIN call_list c ON c.phone = p.phone
                           WHERE c.phone IS NULL ORDER BY RANDOM() LIMIT ?""", (how_many,)).fetchall()
    phones = [r["phone"] for r in rows]
    for ph in phones:
        conn.execute("INSERT INTO call_list(phone, pulled_on, status, updated_on) VALUES(?,?,?,?) "
                     "ON CONFLICT(phone) DO NOTHING", (ph, today, OPEN, today))
    if phones:
        log.info("Pulled %d new numbers from phone_pool into call_list", len(phones))
    else:
        log.warning("phone_pool has no numbers left to pull")
    return phones


def build_today(cfg, conn, today: str, force: bool = False) -> Tuple[int, int]:
    """Draw today's assignment. Returns (numbers assigned, numbers pulled from phone_pool)."""
    surveyors = cfg.active_surveyors()
    if not surveyors:
        raise ConfigError("No active surveyors in config.json")

    existing = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE assign_date=?",
                            (today,)).fetchone()["c"]
    if existing and not force:
        log.info("Assignment for %s already exists (%d numbers); reusing it", today, existing)
        return existing, 0
    if existing:
        log.info("--force: discarding today's existing assignment")
    conn.execute("DELETE FROM assignments")  # only one day's draw is ever kept

    quotas = {s.id: cfg.quota(s) for s in surveyors}
    capacity = sum(quotas.values())

    # 1) candidates already in call_list
    cands = [dict(r) for r in conn.execute(
        "SELECT * FROM call_list WHERE status=? AND (callback_date IS NULL OR callback_date<=?)",
        (OPEN, today))]
    random.shuffle(cands)
    order_of = {"callbacks": 0, "retries": 1, "never called": 2}
    cands.sort(key=lambda r: order_of[_kind(r)])   # stable: random within each group
    chosen = cands[:capacity]

    # 2) top up from phone_pool
    shortfall = capacity - len(chosen)
    pulled: List[str] = []
    if shortfall > 0:
        pulled = pull_numbers(cfg, conn, max(shortfall, cfg.pull_batch_size), today)
        if pulled:
            placeholders = ",".join("?" * len(pulled))
            fresh = [dict(r) for r in conn.execute(
                f"SELECT * FROM call_list WHERE phone IN ({placeholders})", pulled)]
            random.shuffle(fresh)
            chosen += fresh[:shortfall]

    if not chosen:
        log.warning("No numbers left to assign today")
        return 0, len(pulled)

    # 3) callbacks back to their caller, the rest evenly
    load = {sid: 0 for sid in quotas}
    plan, rest = [], []
    for r in chosen:
        sid = r["last_caller"]
        if (cfg.sticky_reschedule and r["last_result_code"] in cfg.reschedule_outcomes
                and sid in load and load[sid] < quotas[sid]):
            plan.append((r, sid))
            load[sid] += 1
        else:
            rest.append(r)
    random.shuffle(rest)
    callers = list(quotas)
    random.shuffle(callers)
    for r in rest:
        open_ids = [sid for sid in callers if load[sid] < quotas[sid]]
        if not open_ids:
            break
        sid = min(open_ids, key=lambda x: load[x] / quotas[x])
        plan.append((r, sid))
        load[sid] += 1

    # 4) today's assignment
    for r, sid in plan:
        conn.execute("INSERT INTO assignments(phone, assign_date, surveyor_id, called) VALUES(?,?,?,0)",
                     (r["phone"], today, sid))

    kinds = {"callbacks": 0, "retries": 0, "never called": 0}
    for r, _ in plan:
        kinds[_kind(r)] += 1
    log.info("Assigned %d numbers to %d callers (%s)", len(plan), len(surveyors),
             ", ".join(f"{v} {k}" for k, v in kinds.items() if v))
    return len(plan), len(pulled)
