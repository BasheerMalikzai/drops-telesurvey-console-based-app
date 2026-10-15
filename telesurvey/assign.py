"""Choosing today's numbers and spreading them across callers.

    phone_pool ──pull──► call_list ──draw──► assignments (today only)

1. Candidates come from call_list: status 'open' whose callback date (if any) has come.
   Order: callbacks due, then other retries, then pulled numbers not called yet,
   random within each group.
2. If they can't fill today's slots, new numbers are pulled at random from phone_pool
   (numbers not yet in call_list), language by language: just the shortfall, or
   pull.batch_size at a time.
3. A number with a caller in the phone list's assigned_to column only ever goes to that
   caller (language is not checked: whoever made the list chose them). If that caller isn't
   an active caller in config.json, the number is treated like any other.
   Other numbers only go to a caller who speaks their language. Rescheduled numbers go back
   to the caller who booked them; otherwise single-language callers are preferred over
   bilingual ones, then the least-loaded caller.
4. assignments is emptied and refilled with today's draw. phone_pool is never changed.
"""
from __future__ import annotations

import logging
import random
from collections import Counter
from typing import List, Tuple

from .config import ConfigError
from .db import OPEN

log = logging.getLogger(__name__)


def _kind(r) -> str:
    if r["callback_date"]:
        return "callbacks"
    return "retries" if (r["attempts"] or r["last_result"]) else "never called"


def _in(column: str, values) -> Tuple[str, list]:
    values = sorted(values)
    return f"{column} IN ({','.join('?' * len(values))})", values


def pull_numbers(cfg, conn, how_many: int, today: str, languages=None,
                 include_unknown: bool = True, owner_keys=None, reserved_keys=None) -> List[str]:
    """Move up to how_many random, not-yet-pulled numbers from phone_pool into call_list.

    languages: only numbers in these languages (None = any language).
    include_unknown: also numbers whose language isn't recorded.
    owner_keys: only numbers assigned to the caller with these keys (language is then ignored).
    reserved_keys: leave out numbers assigned to any of these (other callers' numbers)."""
    if how_many <= 0:
        return []
    where, params = ["c.phone IS NULL"], []
    if owner_keys:
        cond, vals = _in("LOWER(TRIM(p.assigned_to))", owner_keys)
        where.append(cond); params += vals
        languages, include_unknown = None, True
    elif reserved_keys:
        cond, vals = _in("LOWER(TRIM(p.assigned_to))", reserved_keys)
        where.append(f"(p.assigned_to IS NULL OR NOT {cond})"); params += vals
    if languages is not None:
        langs = sorted(languages)
        conds = []
        if langs:
            conds.append(f"p.language IN ({','.join('?' * len(langs))})")
            params += langs
        if include_unknown:
            conds.append("(p.language IS NULL OR p.language = '')")
        if not conds:
            return []
        where.append("(" + " OR ".join(conds) + ")")
    elif not include_unknown:
        where.append("p.language IS NOT NULL AND p.language <> ''")
    rows = conn.execute(f"""SELECT p.phone FROM phone_pool p
                            LEFT JOIN call_list c ON c.phone = p.phone
                            WHERE {' AND '.join(where)} ORDER BY RANDOM() LIMIT ?""",
                        (*params, how_many)).fetchall()
    phones = [r["phone"] for r in rows]
    for ph in phones:
        conn.execute("INSERT INTO call_list(phone, pulled_on, status, updated_on) VALUES(?,?,?,?) "
                     "ON CONFLICT(phone) DO NOTHING", (ph, today, OPEN, today))
    return phones


_CANDIDATE_SQL = """SELECT c.*, COALESCE(p.language, '') AS language,
                           LOWER(TRIM(COALESCE(p.assigned_to, ''))) AS owner_key
                    FROM call_list c LEFT JOIN phone_pool p ON p.phone = c.phone"""


class _Callers:
    """Today's callers, their quotas and how many numbers each has been given so far."""

    def __init__(self, cfg, surveyors):
        self.cfg = cfg
        self.by_id = {s.id: s for s in surveyors}
        self.quota = {s.id: cfg.quota(s) for s in surveyors}
        self.load = {s.id: 0 for s in surveyors}
        self.order = list(self.by_id)
        random.shuffle(self.order)            # random tie-break between equal callers
        self.owner_of = {k: s.id for s in surveyors for k in s.keys()}   # assigned_to value -> caller

    def owner(self, r):
        """The caller this number is reserved for in the phone list, if they're working today."""
        return self.owner_of.get(r.get("owner_key") or "")

    def room(self, sid) -> int:
        return self.quota[sid] - self.load[sid]

    def open_ids(self):
        return [sid for sid in self.order if self.room(sid) > 0]

    def pick(self, r):
        """Caller for number r, or None if nobody who speaks its language has room."""
        owner = self.owner(r)
        if owner is not None:
            return owner if self.room(owner) > 0 else None
        lang = r.get("language") or ""
        sticky = r.get("last_caller")
        if (self.cfg.sticky_reschedule and r.get("last_result_code") in self.cfg.reschedule_outcomes
                and sticky in self.by_id and self.room(sticky) > 0 and self.by_id[sticky].speaks(lang)):
            return sticky
        able = [sid for sid in self.open_ids() if self.by_id[sid].speaks(lang)]
        if not able:
            return None
        # A number with a known language goes to a specialist first (bilingual callers, then
        # callers with no languages set, are kept for numbers only they can take). A number
        # with no language recorded goes the other way: to whoever speaks the most languages.
        # Ties: the least loaded relative to their quota.
        def breadth(sid):
            n = len(self.by_id[sid].languages)
            return n if n else 99
        if lang:
            return min(able, key=lambda sid: (breadth(sid), self.load[sid] / self.quota[sid]))
        return min(able, key=lambda sid: (-breadth(sid), self.load[sid] / self.quota[sid]))

    def wanted_languages(self):
        """Languages that callers with room speak; None if one of them takes any language."""
        langs = set()
        for sid in self.open_ids():
            if not self.by_id[sid].languages:
                return None
            langs.update(self.by_id[sid].languages)
        return langs

    def room_for(self, lang) -> int:
        return sum(self.room(sid) for sid in self.open_ids() if lang in self.by_id[sid].languages)


def _warn_unknown_owners(conn, callers) -> None:
    """assigned_to values that match no active caller: those numbers go to anyone."""
    rows = conn.execute("""SELECT TRIM(p.assigned_to) AS who, COUNT(*) AS n FROM phone_pool p
                           LEFT JOIN call_list c ON c.phone = p.phone
                           WHERE (c.phone IS NULL OR c.status = 'open')
                             AND p.assigned_to IS NOT NULL AND TRIM(p.assigned_to) <> ''
                           GROUP BY TRIM(p.assigned_to)""").fetchall()
    unknown = [(r["who"], r["n"]) for r in rows if r["who"].lower() not in callers.owner_of]
    if unknown:
        log.warning("%d numbers are assigned to callers who aren't active in config.json (%s); "
                    "they go to any caller. Add the caller, or their name as an alias",
                    sum(n for _, n in unknown),
                    ", ".join(f"{w}: {n}" for w, n in sorted(unknown)[:10]) + (" ..." if len(unknown) > 10 else ""))


def _warn_unservable(cfg, conn, surveyors) -> None:
    """Numbers in a language that no active caller speaks can never be assigned: say so."""
    if any(not s.languages for s in surveyors):
        return
    spoken = {lang for s in surveyors for lang in s.languages}
    rows = conn.execute("""SELECT p.language AS lang, COUNT(*) AS n FROM phone_pool p
                           LEFT JOIN call_list c ON c.phone = p.phone
                           WHERE (c.phone IS NULL OR c.status = 'open')
                             AND p.language IS NOT NULL AND p.language <> ''
                             AND (p.assigned_to IS NULL OR TRIM(p.assigned_to) = '')
                           GROUP BY p.language""").fetchall()
    for r in rows:
        if r["lang"] not in spoken:
            log.warning("%d numbers are in '%s', which no active caller speaks; they won't be assigned "
                        "(add the language to a caller in config.json)", r["n"], r["lang"])


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

    callers = _Callers(cfg, surveyors)
    _warn_unservable(cfg, conn, surveyors)
    _warn_unknown_owners(conn, callers)
    reserved = set(callers.owner_of)     # numbers kept for a particular caller
    plan = []

    def place_all(rows):
        placed = 0
        for r in rows:
            if not callers.open_ids():
                break
            sid = callers.pick(r)
            if sid is not None:
                plan.append((r, sid))
                callers.load[sid] += 1
                placed += 1
        return placed

    # 0) make sure each caller's own numbers (assigned_to in the phone list) are in call_list
    pulled: List[str] = []
    for s in surveyors:
        mine, keys = _in("LOWER(TRIM(p.assigned_to))", s.keys())
        have = conn.execute(f"""SELECT COUNT(*) AS n FROM call_list c JOIN phone_pool p ON p.phone = c.phone
                                WHERE c.status=? AND {mine}""", (OPEN, *keys)).fetchone()["n"]
        if have < callers.quota[s.id]:
            pulled += pull_numbers(cfg, conn, callers.quota[s.id] - have, today, owner_keys=s.keys())

    # 1) numbers in call_list: callbacks due, then callers' own numbers, then retries,
    #    then pulled but not called
    cands = [dict(r) for r in conn.execute(
        _CANDIDATE_SQL + " WHERE c.status=? AND (c.callback_date IS NULL OR c.callback_date<=?)",
        (OPEN, today))]
    random.shuffle(cands)
    order_of = {"callbacks": 0, "retries": 2, "never called": 3}
    cands.sort(key=lambda r: 0 if _kind(r) == "callbacks" else
               (1 if callers.owner(r) else order_of[_kind(r)]))   # stable: random within each group
    place_all(cands)

    # 2) top up from phone_pool, language by language, so every caller gets numbers they can call

    def pull_and_place(n, languages, include_unknown):
        got = pull_numbers(cfg, conn, n, today, languages, include_unknown, reserved_keys=reserved)
        if not got:
            return 0
        pulled.extend(got)
        rows = [dict(r) for r in conn.execute(
            _CANDIDATE_SQL + f" WHERE c.phone IN ({','.join('?' * len(got))})", got)]
        random.shuffle(rows)
        return place_all(rows)

    langs = callers.wanted_languages()
    if langs:
        # languages with the fewest callers able to take them first
        for lang in sorted(langs, key=lambda l: sum(1 for s in surveyors if l in s.languages)):
            room = callers.room_for(lang)
            if room > 0:
                pull_and_place(max(room, cfg.pull_batch_size), {lang}, include_unknown=False)
    # whatever room is left: numbers in any language someone with room speaks, or with no language
    for _ in range(3):
        room = sum(callers.room(sid) for sid in callers.open_ids())
        if room <= 0:
            break
        if not pull_and_place(room if langs else max(room, cfg.pull_batch_size),
                              callers.wanted_languages(), include_unknown=True):
            break
    if pulled:
        by_lang = Counter(r["language"] or "no language" for r in conn.execute(
            f"SELECT COALESCE(language, '') AS language FROM phone_pool WHERE phone IN "
            f"({','.join('?' * len(pulled))})", pulled))
        log.info("Pulled %d new numbers from phone_pool into call_list (%s)", len(pulled),
                 ", ".join(f"{k}: {v}" for k, v in sorted(by_lang.items())))
    left = sum(callers.room(sid) for sid in callers.open_ids())
    if left and not plan and not conn.execute("SELECT 1 FROM phone_pool LIMIT 1").fetchone():
        log.warning("The phone pool is empty: check the phone list (%s)", cfg.phone_list)
    elif left:
        short = {sid: callers.room(sid) for sid in callers.open_ids()}
        reason = ("Not enough numbers in the right language" if any(s.languages for s in surveyors)
                  else "Not enough numbers left to call")
        log.warning("%s: %d slots left empty (%s)", reason, left,
                    ", ".join(f"{sid} ({'/'.join(callers.by_id[sid].languages) or 'any'}): {n}"
                              for sid, n in short.items()))

    if not plan:
        log.warning("No numbers left to assign today")
        return 0, len(pulled)

    # 3) today's assignment
    for r, sid in plan:
        conn.execute("INSERT INTO assignments(phone, assign_date, surveyor_id, called) VALUES(?,?,?,0)",
                     (r["phone"], today, sid))

    own = sum(1 for r, sid in plan if callers.owner(r) == sid)
    if own:
        log.info("%d numbers went to the caller named in the phone list's assigned_to column", own)
    kinds = {"callbacks": 0, "retries": 0, "never called": 0}
    for r, _ in plan:
        kinds[_kind(r)] += 1
    per_lang = Counter(r.get("language") or "no language" for r, _ in plan)
    log.info("Assigned %d numbers to %d callers (%s; %s)", len(plan), len(surveyors),
             ", ".join(f"{v} {k}" for k, v in kinds.items() if v),
             ", ".join(f"{k}: {v}" for k, v in sorted(per_lang.items())))
    return len(plan), len(pulled)
