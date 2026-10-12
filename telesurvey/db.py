"""Database layer: PostgreSQL (default) or SQLite.

Tables
  phone_pool   master list of numbers; only ever added to by the import, never changed
  call_list    numbers pulled from the pool for calling: status, last result, attempts,
               first/last contact dates. This is where every result is recorded.
  assignments  today's run only: number, caller, date, called yet? Rebuilt each morning.
  submissions  one row per KoBo submission (the full call history)
  meta         small internal values

All SQL in the package uses '?' placeholders and ON CONFLICT clauses that both
engines understand; this module translates where needed.
"""
from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# Arbitrary fixed numbers identifying our PostgreSQL advisory locks
RUN_LOCK_KEY = 7245001     # daily run vs. sync job
WRITE_LOCK_KEY = 7245002   # serialises write transactions

# call_list.status values
OPEN, REVIEW, DONE, EXHAUSTED, REMOVED = "open", "review", "done", "exhausted", "removed"
CLOSED_STATUSES = (DONE, EXHAUSTED, REMOVED)


def _schema(serial: str, bigint: str) -> list:
    return [
        """CREATE TABLE IF NOT EXISTS phone_pool(
            phone TEXT PRIMARY KEY,
            contact_name TEXT,
            added_on TEXT)""",
        """CREATE TABLE IF NOT EXISTS call_list(
            phone TEXT PRIMARY KEY,
            pulled_on TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_result TEXT,
            last_result_code TEXT,
            last_caller TEXT,
            first_contacted_on TEXT,
            last_contacted_on TEXT,
            last_contacted_at TEXT,
            callback_date TEXT,
            updated_on TEXT)""",
        """CREATE TABLE IF NOT EXISTS assignments(
            phone TEXT PRIMARY KEY,
            assign_date TEXT NOT NULL,
            surveyor_id TEXT NOT NULL,
            called INTEGER NOT NULL DEFAULT 0)""",
        f"""CREATE TABLE IF NOT EXISTS submissions(
            kobo_id {bigint} PRIMARY KEY, phone TEXT, surveyor_id TEXT, outcome TEXT,
            submitted_at TEXT, submit_date TEXT, validation TEXT,
            applied INTEGER NOT NULL DEFAULT 1)""",
        "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
        "CREATE INDEX IF NOT EXISTS idx_cl_status ON call_list(status)",
        "CREATE INDEX IF NOT EXISTS idx_sub_date ON submissions(submit_date)",
        "CREATE INDEX IF NOT EXISTS idx_sub_phone ON submissions(phone)",
    ]


SCHEMA_SQLITE = _schema("INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER")
SCHEMA_POSTGRES = _schema("BIGSERIAL PRIMARY KEY", "BIGINT")

TABLES = ["phone_pool", "call_list", "assignments", "submissions", "meta"]

# Columns added to submissions after the first release (older databases get them automatically).
# applied defaults to 1 so submissions recorded by older versions count as already applied.
NEW_COLUMNS = {
    "submissions": [("validation", "TEXT"),
                    ("applied", "INTEGER NOT NULL DEFAULT 1"),
                    ("submit_date", "TEXT")],
}

# Older versions kept the number status in phone_pool; map it onto call_list.status
LEGACY_STATUS = {"assigned": OPEN, "pending": OPEN, "review": REVIEW, "done": DONE, "exhausted": EXHAUSTED}


class DatabaseError(Exception):
    pass


class Database:
    """Small wrapper so the rest of the code doesn't care which engine is used."""

    def __init__(self, cfg, create_schema: bool = True):
        self.cfg = cfg
        self.engine = cfg.db_engine
        if self.engine == "postgres":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as e:
                raise DatabaseError("psycopg is not installed: run ./setup.sh again") from e
            try:
                self.conn = psycopg.connect(**cfg.pg_connect_kwargs(), row_factory=dict_row)
            except psycopg.OperationalError as e:
                reason = " ".join(str(e).split())   # psycopg spreads the reason over several lines
                raise DatabaseError(f"Cannot connect to {cfg.db_description}: {reason[:400]}") from e
            if create_schema and cfg.pg.get("schema"):
                self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{cfg.pg["schema"]}"')
        else:
            cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(cfg.db_path), timeout=60)
            self.conn.row_factory = sqlite3.Row
            wal = os.environ.get("SQLITE_WAL", "1") not in ("0", "false", "no")
            self.conn.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
        if create_schema:
            self._create_and_upgrade()
            self.conn.commit()

    # -- schema --------------------------------------------------------------
    def _tables(self) -> set:
        if self.engine == "postgres":
            rows = self.conn.execute("SELECT table_name AS t FROM information_schema.tables "
                                     "WHERE table_schema=current_schema()").fetchall()
        else:
            rows = self.conn.execute("SELECT name AS t FROM sqlite_master WHERE type='table'").fetchall()
        return {r["t"] for r in rows}

    def _columns(self, table: str) -> set:
        if self.engine == "postgres":
            rows = self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s AND table_schema=current_schema()", (table,)).fetchall()
            return {r["column_name"] for r in rows}
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _create_and_upgrade(self) -> None:
        tables = self._tables()
        # 1. the old assignments table (one row per number per day) becomes assignments_legacy
        legacy_asg = "assignments" in tables and "closed" in self._columns("assignments")
        if legacy_asg:
            if "assignments_legacy" in tables:
                self.conn.execute("DROP TABLE assignments_legacy")
            self.conn.execute("ALTER TABLE assignments RENAME TO assignments_legacy")
            log.info("Database upgrade: old assignments table kept as assignments_legacy")

        for stmt in (SCHEMA_POSTGRES if self.engine == "postgres" else SCHEMA_SQLITE):
            self.conn.execute(stmt)
        for table, cols in NEW_COLUMNS.items():
            have = self._columns(table)
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    log.info("Database upgrade: added column %s.%s", table, name)

        if legacy_asg:
            # keep the most recent day's draw so today's run can reuse it
            self.execute("""INSERT INTO assignments(phone, assign_date, surveyor_id, called)
                            SELECT phone, assign_date, surveyor_id,
                                   CASE WHEN outcome IS NULL THEN 0 ELSE 1 END
                            FROM assignments_legacy
                            WHERE closed=0 AND assign_date=(SELECT MAX(assign_date) FROM assignments_legacy)
                            ON CONFLICT(phone) DO NOTHING""")

        # 2. number states stored in phone_pool by older versions move to call_list (once)
        if "status" in self._columns("phone_pool") and get_meta(self, "call_list_migrated") is None:
            rows = self.execute("SELECT * FROM phone_pool WHERE status <> 'new'").fetchall()
            n = migrate_legacy_pool(self, [dict(r) for r in rows])
            set_meta(self, "call_list_migrated", n)
            log.info("Database upgrade: %d numbers moved into call_list; phone_pool is no longer changed", n)

    # -- basic operations ----------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()):
        if self.engine == "postgres":
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, tuple(params))

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def lock_writes(self):
        """Call first in a write transaction. On PostgreSQL this makes the daily run and the
        sync job take turns instead of risking a deadlock; SQLite already works this way."""
        if self.engine == "postgres":
            self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))


def migrate_legacy_pool(conn: "Database", rows: list) -> int:
    """Create call_list rows from old-style phone_pool rows (which carried a status)."""
    n = 0
    for r in rows:
        status = LEGACY_STATUS.get(r.get("status") or "")
        if not status:
            continue
        dates = conn.execute("SELECT MIN(submit_date) AS f, MAX(submit_date) AS l, MAX(submitted_at) AS t "
                             "FROM submissions WHERE phone=? AND applied=1", (r["phone"],)).fetchone()
        code = r.get("last_outcome")
        n += conn.execute(
            """INSERT INTO call_list(phone, pulled_on, status, attempts, last_result, last_result_code,
                   last_caller, first_contacted_on, last_contacted_on, last_contacted_at, callback_date, updated_on)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(phone) DO NOTHING""",
            (r["phone"], r.get("updated_on") or r.get("added_on") or "", status, r.get("attempts") or 0,
             conn.cfg.category_of(code) if code else None, code, r.get("last_surveyor"),
             dates["f"], dates["l"], dates["t"], r.get("callback_date"), r.get("updated_on"))).rowcount
    return n


def connect(cfg, create_schema: bool = True) -> Database:
    return Database(cfg, create_schema=create_schema)


def get_meta(conn: Database, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: Database, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


@contextmanager
def run_lock(cfg, wait_seconds: float = 0):
    """Only one of {daily run, sync} at a time. Yields True if the lock was taken.

    PostgreSQL: a session advisory lock (works across machines, e.g. PythonAnywhere
    tasks and web apps). SQLite: a lock file next to the database.
    """
    deadline = time.time() + wait_seconds
    if cfg.db_engine == "postgres":
        import psycopg
        try:
            conn = psycopg.connect(**cfg.pg_connect_kwargs(), autocommit=True)
        except psycopg.OperationalError as e:
            raise DatabaseError(f"Cannot connect to {cfg.db_description}: {' '.join(str(e).split())[:400]}") from e
        try:
            while True:
                got = conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_KEY,)).fetchone()[0]
                if got or time.time() >= deadline:
                    break
                time.sleep(2)
            yield got
        finally:
            conn.close()  # closing the session releases the lock
    else:
        cfg.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.lock_file, "a") as f:
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except BlockingIOError:
                    if time.time() >= deadline:
                        got = False
                        break
                    time.sleep(2)
            try:
                yield got
            finally:
                if got:
                    fcntl.flock(f, fcntl.LOCK_UN)



def copy_sqlite_to(cfg, sqlite_path) -> dict:
    """Copy an existing SQLite database (old or new layout) into the configured database."""
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    dst = connect(cfg)
    src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    copied = {}
    keys = {"phone_pool": "phone", "call_list": "phone", "assignments": "phone",
            "submissions": "kobo_id", "meta": "key"}
    try:
        dst.lock_writes()
        for table in TABLES:
            if table not in src_tables:
                continue
            src_cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
            if table == "assignments" and "closed" in src_cols:
                continue  # old per-day history: not needed, today's draw is rebuilt
            cols = [c for c in dst._columns(table) if c in src_cols]
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            sql = (f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
                   f"ON CONFLICT({keys[table]}) DO NOTHING")
            copied[table] = sum(dst.execute(sql, [r[c] for c in cols]).rowcount for r in rows)
        if "call_list" not in src_tables and "phone_pool" in src_tables:
            src_cols = {r[1] for r in src.execute("PRAGMA table_info(phone_pool)")}
            if "status" in src_cols:  # old layout: number states lived in phone_pool
                rows = [dict(r) for r in src.execute("SELECT * FROM phone_pool WHERE status <> 'new'")]
                copied["call_list"] = migrate_legacy_pool(dst, rows)
        dst.commit()
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()
    return copied
