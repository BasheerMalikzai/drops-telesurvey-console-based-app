"""Load settings from config.json (callers, rules) and .env (KoBo connection)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Default call-result categories. Each category lists the KoBo choice codes that belong to
# it and what happens to the number. They are also the columns of the email report.
#   done     = finished for good        retry = try again on a later day
#   callback = try again on the callback date, with the same caller
DEFAULT_OUTCOMES = {
    "successful":  {"action": "done",     "codes": ["completed", "successful", "success"]},
    "rescheduled": {"action": "callback", "codes": ["rescheduled", "reschedule", "callback"]},
    "declined":    {"action": "done",     "codes": ["refused", "declined", "decline"]},
    "no_pickup":   {"action": "retry",    "codes": ["no_answer", "not_picked", "no_pickup", "unanswered"]},
    "busy":        {"action": "retry",    "codes": ["busy"]},
    "invalid":     {"action": "done",     "codes": ["invalid_number", "wrong_number"]},
}
CATEGORY_LABELS = {"successful": "Successful", "rescheduled": "Rescheduled", "declined": "Declined",
                   "no_pickup": "Didn't pick up", "busy": "Busy", "invalid": "Invalid number"}
VALID_ACTIONS = ("done", "retry", "callback")

# KoBo validation statuses
VALIDATION_APPROVED = "validation_status_approved"
VALIDATION_REJECTED = "validation_status_not_approved"

# How often the sync job runs during working hours (download results, refresh the call list)
REFRESH_MIN_MINUTES = 20
REFRESH_MAX_MINUTES = 360
DEFAULT_REFRESH_MINUTES = 30
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ConfigError(Exception):
    pass


@dataclass
class Surveyor:
    id: str
    name: str = ""
    phone: str = ""
    active: bool = True
    daily_quota: Optional[int] = None


@dataclass
class Config:
    project_dir: Path
    db_path: Path
    phone_list: Path
    output_dir: Path
    log_file: Path

    numbers_per_surveyor: int
    max_attempts: int                 # 0 = no limit
    sticky_reschedule: bool

    final_outcomes: set
    retry_outcomes: set
    reschedule_outcomes: set
    categories: dict          # name -> {"action": ..., "codes": set, "label": ...}

    kobo_url: str
    kobo_token: str
    asset_uid: str
    media_filename: str
    redeploy: bool

    field_phone: str
    field_status: str
    field_surveyor: str
    field_callback: str
    field_surveyor_fallback: str = "_submitted_by"
    field_validation: str = "_validation_status"
    field_submission_time: str = "_submission_time"

    reschedules_count_as_attempts: bool = False
    pull_batch_size: int = 0          # 0 = pull just enough new numbers to fill each day

    required_validation: str = ""   # "" = every submission counts, whatever its validation status
    lookback_days: int = 14          # first download (and approval mode): how far back to read
    overlap_hours: int = 24          # each download re-reads this much before the last one

    report_enabled: bool = True
    report_recipients: List[str] = field(default_factory=list)
    report_send_on: str = "always"                   # always | failure | never
    report_subject_prefix: str = "[Telesurvey]"
    report_day: str = "yesterday"                    # yesterday | today
    smtp: dict = field(default_factory=dict)

    db_engine: str = "postgres"          # postgres | sqlite
    pg: dict = field(default_factory=dict)

    # schedule (installed with ./setup.sh --schedule)
    morning_time: str = "06:00"           # the daily run: draw, upload, email report
    sync_every_minutes: int = DEFAULT_REFRESH_MINUTES   # 20-360, 0 = no syncing during the day
    sync_from: str = "07:00"              # syncs only run between these times ...
    sync_until: str = "19:00"
    run_days: List[str] = field(default_factory=lambda: list(DAY_NAMES))   # ... on these days
    sync_overlap_minutes: int = 60        # each sync re-reads this much before the last download
    config_path: Optional[Path] = None

    surveyors: List[Surveyor] = field(default_factory=list)

    @property
    def kobo_enabled(self) -> bool:
        return bool(self.kobo_token and self.asset_uid)

    def pg_connect_kwargs(self) -> dict:
        """Arguments for psycopg.connect(). DATABASE_URL wins over the separate PG* settings."""
        kw = {"connect_timeout": 15}
        if self.pg.get("url"):
            kw["conninfo"] = self.pg["url"]
        else:
            for key in ("host", "port", "dbname", "user", "password", "sslmode"):
                if self.pg.get(key):
                    kw[key] = self.pg[key]
        if self.pg.get("schema"):
            kw["options"] = f"-c search_path={self.pg['schema']}"
        return kw

    @property
    def db_description(self) -> str:
        """Where the data lives, without the password (for logs and messages)."""
        if self.db_engine != "postgres":
            return f"SQLite {self.db_path}"
        if self.pg.get("url"):
            from urllib.parse import urlsplit
            u = urlsplit(self.pg["url"])
            return f"PostgreSQL {u.username or ''}@{u.hostname or ''}:{u.port or 5432}{u.path}"
        schema = f" (schema {self.pg['schema']})" if self.pg.get("schema") else ""
        return (f"PostgreSQL {self.pg.get('user', '')}@{self.pg.get('host', 'localhost')}:"
                f"{self.pg.get('port', 5432)}/{self.pg.get('dbname', '')}{schema}")

    @property
    def lock_file(self) -> Path:
        """Shared by the daily run and the sync job so they never overlap (SQLite only)."""
        return self.project_dir / "data" / ".daily.lock"

    @property
    def api_base(self) -> str:
        return f"{self.kobo_url.rstrip('/')}/api/v2/assets/{self.asset_uid}"

    def category_of(self, code: str) -> str:
        """Category name for a KoBo call-result code ('other' if it isn't listed)."""
        code = (code or "").strip().lower()
        for name, c in self.categories.items():
            if code in c["codes"]:
                return name
        return "other"

    def surveyor_name(self, sid: str) -> str:
        for s in self.surveyors:
            if s.id == sid:
                return s.name
        return ""

    @property
    def email_ready(self) -> bool:
        return bool(self.report_recipients and self.smtp.get("host"))

    def quota(self, s: Surveyor) -> int:
        return s.daily_quota if s.daily_quota is not None else self.numbers_per_surveyor

    def active_surveyors(self) -> List[Surveyor]:
        return [s for s in self.surveyors if s.active and self.quota(s) > 0]


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=value lines). Existing environment variables win."""
    if not path.exists():
        return
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        # unwrap matching quotes only, so passwords may contain quotes, spaces, # and $
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value   # a later line wins, like in the shell
    for key, value in values.items():
        if value != "":   # a blank line in .env means "not set", not "empty"
            os.environ.setdefault(key, value)
    # TZ=Africa/Accra (etc.) makes "today" your local day even on a UTC server
    if os.environ.get("TZ") and hasattr(time, "tzset"):
        time.tzset()


def _path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def _lower_set(values) -> set:
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


def load_config(config_path: Optional[Path] = None) -> Config:
    project = PROJECT_DIR
    load_dotenv(project / ".env")

    config_path = Path(config_path or os.environ.get("TELESURVEY_CONFIG") or project / "config.json")
    if not config_path.exists():
        raise ConfigError(f"{config_path} not found. Run ./setup.sh first.")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{config_path} is not valid JSON: {e}") from e

    paths = raw.get("paths", {})
    rules = raw.get("assignment", {})
    categories = parse_outcomes(raw.get("outcomes"))
    fields = raw.get("kobo_fields", {})
    kimport = raw.get("kobo_import", {})
    report = raw.get("report", {})
    sched = raw.get("schedule", {})
    legacy_hook = raw.get("webhook", {})   # older versions kept the interval here

    surveyors = []
    for i, s in enumerate(raw.get("surveyors", []), 1):
        sid = str(s.get("id", "")).strip()
        if not sid:
            raise ConfigError(f"Surveyor #{i} in config.json has no 'id'")
        quota = s.get("daily_quota")
        surveyors.append(Surveyor(id=sid, name=str(s.get("name", "")), phone=str(s.get("phone", "")),
                                  active=bool(s.get("active", True)),
                                  daily_quota=int(quota) if quota is not None else None))

    cfg = Config(
        project_dir=project,
        db_path=_path(project, paths.get("database", "data/telesurvey.db")),
        phone_list=_path(project, paths.get("phone_list", "data/phone_numbers.csv")),
        output_dir=_path(project, paths.get("output_dir", "data/assignments")),
        log_file=_path(project, paths.get("log_file", "data/telesurvey.log")),
        numbers_per_surveyor=int(rules.get("numbers_per_surveyor", 30)),
        max_attempts=int(rules.get("max_attempts", 5)),
        sticky_reschedule=bool(rules.get("sticky_reschedule", True)),
        reschedules_count_as_attempts=bool(rules.get("reschedules_count_as_attempts", False)),
        pull_batch_size=int(raw.get("pull", {}).get("batch_size", 0)),
        final_outcomes={c for v in categories.values() if v["action"] == "done" for c in v["codes"]},
        retry_outcomes={c for v in categories.values() if v["action"] != "done" for c in v["codes"]},
        reschedule_outcomes={c for v in categories.values() if v["action"] == "callback" for c in v["codes"]},
        categories=categories,
        kobo_url=os.environ.get("KOBO_URL", "https://kf.kobotoolbox.org"),
        kobo_token=os.environ.get("KOBO_TOKEN", ""),
        asset_uid=os.environ.get("KOBO_ASSET_UID", ""),
        media_filename=os.environ.get("KOBO_MEDIA_FILENAME", "assignments.csv"),
        redeploy=os.environ.get("KOBO_REDEPLOY", "1") not in ("0", "false", "no"),
        field_phone=fields.get("phone", "phnumber"),
        field_status=fields.get("call_status", "recording_group/status_call"),
        field_surveyor=fields.get("surveyor", "idnumber"),
        field_callback=fields.get("callback_date", "reschedual"),
        field_surveyor_fallback=fields.get("surveyor_fallback", "_submitted_by"),
        field_validation=fields.get("validation_status", "_validation_status"),
        field_submission_time=fields.get("submission_time", "_submission_time"),
        required_validation=str(kimport.get("required_validation_status", "")).strip(),
        lookback_days=int(kimport.get("lookback_days", 14)),
        overlap_hours=int(kimport.get("overlap_hours", 24)),
        report_enabled=bool(report.get("enabled", True)),
        report_recipients=[str(r).strip() for r in report.get("recipients", []) if str(r).strip()],
        report_send_on=str(report.get("send_on", "always")).strip().lower(),
        report_subject_prefix=str(report.get("subject_prefix", "[Telesurvey]")),
        report_day=str(report.get("report_day", "yesterday")).strip().lower(),
        smtp={"host": os.environ.get("SMTP_HOST", ""),
              "port": os.environ.get("SMTP_PORT", ""),
              "security": os.environ.get("SMTP_SECURITY", "starttls").strip().lower(),
              "user": os.environ.get("SMTP_USER", ""),
              "password": os.environ.get("SMTP_PASSWORD", ""),
              "sender": os.environ.get("SMTP_FROM", "")},
        db_engine=os.environ.get("DB_ENGINE", "postgres").strip().lower(),
        pg={"url": os.environ.get("DATABASE_URL", ""),
            "host": os.environ.get("PGHOST", "localhost"),
            "port": os.environ.get("PGPORT", "5432"),
            "dbname": os.environ.get("PGDATABASE", "telesurvey"),
            "user": os.environ.get("PGUSER", ""),
            "password": os.environ.get("PGPASSWORD", ""),
            "sslmode": os.environ.get("PGSSLMODE", "prefer"),
            "schema": os.environ.get("PG_SCHEMA", "")},
        morning_time=check_time(sched.get("morning_run", "06:00"), "schedule.morning_run"),
        sync_every_minutes=check_refresh_minutes(
            sched.get("sync_every_minutes", legacy_hook.get("refresh_minutes", DEFAULT_REFRESH_MINUTES))),
        sync_from=check_time(sched.get("sync_from", "07:00"), "schedule.sync_from"),
        sync_until=check_time(sched.get("sync_until", "19:00"), "schedule.sync_until"),
        run_days=check_days(sched.get("days", DAY_NAMES)),
        sync_overlap_minutes=int(sched.get("sync_overlap_minutes", 60)),
        config_path=config_path,
        surveyors=surveyors,
    )
    validate(cfg)
    return cfg


def parse_outcomes(raw) -> dict:
    """Read the 'outcomes' section. Also accepts the older final/retry/reschedule lists."""
    if not raw:
        raw = DEFAULT_OUTCOMES
    if isinstance(raw.get("final"), list) or isinstance(raw.get("retry"), list):
        # older format: three lists of codes
        resched = _lower_set(raw.get("reschedule", ["rescheduled"]))
        raw = {"done": {"action": "done", "codes": list(raw.get("final", []))},
               "rescheduled": {"action": "callback", "codes": list(resched)},
               "retry": {"action": "retry", "codes": [c for c in raw.get("retry", []) if c not in resched]}}
    out, seen = {}, {}
    for name, spec in raw.items():
        if name.startswith("_"):
            continue  # allow "_comment" keys
        if not isinstance(spec, dict):
            raise ConfigError(f"outcomes.{name} must be an object with 'action' and 'codes'")
        action = str(spec.get("action", "")).strip().lower()
        if action not in VALID_ACTIONS:
            raise ConfigError(f"outcomes.{name}.action must be one of {', '.join(VALID_ACTIONS)}, not {action!r}")
        codes = _lower_set(spec.get("codes", []))
        for c in codes:
            if c in seen:
                raise ConfigError(f"Call-result code '{c}' is listed in both outcomes.{seen[c]} and outcomes.{name}")
            seen[c] = name
        out[name] = {"action": action, "codes": codes,
                     "label": spec.get("label") or CATEGORY_LABELS.get(name, name.replace("_", " ").capitalize())}
    return out


def save_recipients(cfg: Config, recipients) -> List[str]:
    """Write report.recipients into config.json."""
    import re
    emails = [r.strip() for r in recipients if r.strip()]
    bad = [e for e in emails if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
    if bad:
        raise ConfigError(f"Not a valid email address: {', '.join(bad)}")
    path = cfg.config_path or (cfg.project_dir / "config.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.setdefault("report", {})["recipients"] = emails
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    cfg.report_recipients = emails
    return emails


def check_refresh_minutes(value) -> int:
    """20-360 minutes, or 0 to switch syncing during the day off."""
    try:
        minutes = int(value)
        if minutes != float(value):
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigError(f"sync_every_minutes must be a whole number of minutes, not {value!r}")
    if minutes != 0 and not REFRESH_MIN_MINUTES <= minutes <= REFRESH_MAX_MINUTES:
        raise ConfigError(f"sync_every_minutes must be between {REFRESH_MIN_MINUTES} and "
                          f"{REFRESH_MAX_MINUTES} (or 0 to switch syncing off), not {minutes}")
    return minutes


def check_time(value, name: str) -> str:
    """'6:00' / '06:00' -> '06:00'."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        raise ConfigError(f"{name} must be a time like 06:00, not {value!r}")
    return f"{h:02d}:{m:02d}"


def check_days(value) -> List[str]:
    if isinstance(value, str):
        value = [v for v in value.replace(",", " ").split()]
    days = [str(v).strip().lower()[:3] for v in value]
    bad = [d for d in days if d not in DAY_NAMES]
    if bad or not days:
        raise ConfigError(f"schedule.days must list days like [\"mon\", \"tue\"], not {value!r}")
    return [d for d in DAY_NAMES if d in days]


def _save_config_value(cfg: Config, section: str, key: str, value) -> None:
    path = cfg.config_path or (cfg.project_dir / "config.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.setdefault(section, {})[key] = value
    raw.pop("webhook", None)  # the old live-update section is no longer used
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_schedule(cfg: Config, morning=None, sync_from=None, sync_until=None, days=None) -> None:
    """Validate and write schedule times / days into config.json."""
    updates = {}
    if morning is not None:
        updates["morning_run"] = check_time(morning, "morning run time")
    if sync_from is not None:
        updates["sync_from"] = check_time(sync_from, "sync start time")
    if sync_until is not None:
        updates["sync_until"] = check_time(sync_until, "sync end time")
    if days is not None:
        updates["days"] = check_days(days)
    start = updates.get("sync_from", cfg.sync_from)
    end = updates.get("sync_until", cfg.sync_until)
    if start >= end:
        raise ConfigError(f"Sync start ({start}) must be earlier than sync end ({end})")
    for k, v in updates.items():
        _save_config_value(cfg, "schedule", k, v)
    cfg.morning_time = updates.get("morning_run", cfg.morning_time)
    cfg.sync_from, cfg.sync_until = start, end
    cfg.run_days = updates.get("days", cfg.run_days)


def save_refresh_minutes(cfg: Config, value) -> int:
    """Write schedule.sync_every_minutes into config.json, keeping everything else as it is."""
    minutes = check_refresh_minutes(value)
    _save_config_value(cfg, "schedule", "sync_every_minutes", minutes)
    cfg.sync_every_minutes = minutes
    return minutes


def validate(cfg: Config) -> None:
    ids = [s.id for s in cfg.surveyors]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"Duplicate surveyor ids in config.json: {', '.join(sorted(dupes))}")
    if cfg.numbers_per_surveyor < 1:
        raise ConfigError("assignment.numbers_per_surveyor must be at least 1")
    if cfg.max_attempts < 0:
        raise ConfigError("assignment.max_attempts must be 0 (no limit) or more")
    if cfg.pull_batch_size < 0:
        raise ConfigError("pull.batch_size must be 0 (just enough each day) or more")
    if cfg.db_engine not in ("postgres", "sqlite"):
        raise ConfigError(f"DB_ENGINE must be 'postgres' or 'sqlite', not '{cfg.db_engine}'")
    if cfg.db_engine == "postgres" and not (cfg.pg.get("url") or cfg.pg.get("user")):
        raise ConfigError("PostgreSQL user not set: fill in PGUSER/PGPASSWORD in .env "
                          "(or run ./setup.sh --database)")
    if cfg.pg.get("schema") and not cfg.pg["schema"].replace("_", "").isalnum():
        raise ConfigError("PG_SCHEMA may only contain letters, digits and underscores")
    if cfg.report_send_on not in ("always", "failure", "never"):
        raise ConfigError("report.send_on must be 'always', 'failure' or 'never'")
    if cfg.report_day not in ("yesterday", "today"):
        raise ConfigError("report.report_day must be 'yesterday' or 'today'")
    if cfg.lookback_days < 1:
        raise ConfigError("kobo_import.lookback_days must be at least 1")
    if cfg.overlap_hours < 1:
        raise ConfigError("kobo_import.overlap_hours must be at least 1")
    if cfg.sync_overlap_minutes < 5:
        raise ConfigError("schedule.sync_overlap_minutes must be at least 5")
    if cfg.sync_from >= cfg.sync_until:
        raise ConfigError("schedule.sync_from must be earlier than schedule.sync_until")
