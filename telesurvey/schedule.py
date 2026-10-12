"""Installing the two scheduled jobs.

  morning job  daily.sh at schedule.morning_run, on schedule.days
  sync job     sync.sh every 5 minutes; the sync itself decides whether there is work
               (sync hours, sync_every_minutes, and a missed morning run to catch up on)

macOS: launchd agents in ~/Library/LaunchAgents. Unlike cron, launchd runs a job that was
       due while the Mac was asleep as soon as it wakes. Jobs run under caffeinate so the
       Mac doesn't sleep in the middle of a run.
Linux: entries in the user's crontab.
"""
from __future__ import annotations

import datetime as dt
import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from .config import DAY_NAMES

TICK_MINUTES = 5
PMSET_DAYS = {"mon": "M", "tue": "T", "wed": "W", "thu": "R", "fri": "F", "sat": "S", "sun": "U"}
PROTECTED = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")


class ScheduleError(Exception):
    pass


def is_mac() -> bool:
    return platform.system() == "Darwin"


def _label(cfg, job: str) -> str:
    name = "".join(c if c.isalnum() else "-" for c in cfg.project_dir.name.lower()).strip("-") or "telesurvey"
    return f"org.telesurvey.{name}.{job}"


def _plist_path(cfg, job: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(cfg, job)}.plist"


def _scripts(cfg):
    daily, sync = cfg.project_dir / "daily.sh", cfg.project_dir / "sync.sh"
    for p in (daily, sync):
        if not p.exists():
            raise ScheduleError(f"{p} is missing")
        os.chmod(p, 0o755)
    return daily, sync


def _hm(t: str):
    h, m = t.split(":")
    return int(h), int(m)


def macos_warning(cfg) -> str:
    """macOS blocks background jobs from Documents, Desktop, Downloads and iCloud Drive."""
    home = Path.home()
    for d in PROTECTED:
        try:
            cfg.project_dir.resolve().relative_to((home / d).resolve())
            return (f"WARNING: the project is inside ~/{d}. macOS stops scheduled jobs from reading "
                    f"that folder. Move it, e.g. to ~/telesurvey, then run the install again.")
        except ValueError:
            continue
    return ""


# --------------------------------------------------------------------------- macOS

def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _load(plist: Path) -> None:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(plist))           # ignore "not loaded"
    r = _launchctl("bootstrap", domain, str(plist))
    if r.returncode != 0:                                # older macOS
        r = _launchctl("load", "-w", str(plist))
        if r.returncode != 0:
            raise ScheduleError(f"launchctl could not load {plist.name}: {(r.stderr or r.stdout).strip()}")


def _unload(plist: Path) -> None:
    if plist.exists():
        _launchctl("bootout", f"gui/{os.getuid()}", str(plist))
        _launchctl("unload", "-w", str(plist))
        plist.unlink()


def _mac_plists(cfg):
    daily, sync = _scripts(cfg)
    logs = cfg.project_dir / "data"
    logs.mkdir(parents=True, exist_ok=True)
    h, m = _hm(cfg.morning_time)
    # launchd weekdays: 0 = Sunday, 1 = Monday ... 6 = Saturday
    when = [{"Hour": h, "Minute": m, "Weekday": (DAY_NAMES.index(d) + 1) % 7} for d in cfg.run_days]
    if len(when) == 7:
        when = [{"Hour": h, "Minute": m}]
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    common = {"WorkingDirectory": str(cfg.project_dir), "EnvironmentVariables": env,
              "ProcessType": "Background", "RunAtLoad": False}
    return {
        "daily": dict(common, Label=_label(cfg, "daily"),
                      ProgramArguments=["/usr/bin/caffeinate", "-i", str(daily), "--scheduled"],
                      StartCalendarInterval=when,
                      StandardOutPath=str(logs / "launchd-daily.log"),
                      StandardErrorPath=str(logs / "launchd-daily.log")),
        "sync": dict(common, Label=_label(cfg, "sync"),
                     ProgramArguments=["/usr/bin/caffeinate", "-i", str(sync)],
                     StartInterval=TICK_MINUTES * 60,
                     StandardOutPath=str(logs / "launchd-sync.log"),
                     StandardErrorPath=str(logs / "launchd-sync.log")),
    }


# --------------------------------------------------------------------------- Linux

def _marker(cfg) -> str:
    return f"# telesurvey:{cfg.project_dir}"


def _cron_lines(cfg):
    daily, sync = _scripts(cfg)
    h, m = _hm(cfg.morning_time)
    dow = ",".join(str((DAY_NAMES.index(d) + 1) % 7) for d in cfg.run_days) if len(cfg.run_days) < 7 else "*"
    log = cfg.project_dir / "data" / "cron.log"
    mk = _marker(cfg)
    return [f"{m} {h} * * {dow} {daily} --scheduled >> {log} 2>&1 {mk}",
            f"*/{TICK_MINUTES} * * * * {sync} >> {log} 2>&1 {mk}"]


def _crontab_read() -> list:
    if not shutil.which("crontab"):
        raise ScheduleError("crontab is not installed (Ubuntu/Debian: sudo apt install cron)")
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else []


def _crontab_write(lines: list) -> None:
    r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if r.returncode != 0:
        raise ScheduleError(f"Could not update the crontab: {r.stderr.strip()}")


# --------------------------------------------------------------------------- public

def installed(cfg) -> bool:
    if is_mac():
        return _plist_path(cfg, "daily").exists()
    try:
        return any(_marker(cfg) in line for line in _crontab_read())
    except ScheduleError:
        return False


def install(cfg) -> str:
    if is_mac():
        warn = macos_warning(cfg)
        if warn:
            raise ScheduleError(warn)
        for job, data in _mac_plists(cfg).items():
            path = _plist_path(cfg, job)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                plistlib.dump(data, f)
            _load(path)
        how = "launchd (runs missed jobs when the Mac wakes)"
    else:
        keep = [line for line in _crontab_read() if _marker(cfg) not in line]
        _crontab_write(keep + _cron_lines(cfg))
        how = "cron"
    return (f"Scheduled with {how}: morning run at {cfg.morning_time} on {', '.join(cfg.run_days)}; "
            + (f"sync every {cfg.sync_every_minutes} min, {cfg.sync_from}-{cfg.sync_until}"
               if cfg.sync_every_minutes else "no syncing during the day"))


def remove(cfg) -> str:
    if is_mac():
        for job in ("daily", "sync"):
            _unload(_plist_path(cfg, job))
    else:
        _crontab_write([line for line in _crontab_read() if _marker(cfg) not in line])
    return "Schedule removed (nothing will run automatically)"


def wake_command(cfg) -> str:
    """pmset command that wakes (or starts) the Mac 5 minutes before the morning run."""
    h, m = _hm(cfg.morning_time)
    t = (dt.datetime(2000, 1, 1, h, m) - dt.timedelta(minutes=5)).strftime("%H:%M:00")
    days = "".join(PMSET_DAYS[d] for d in cfg.run_days)
    return f"pmset repeat wakeorpoweron {days} {t}"


def describe(cfg) -> str:
    from .sync import load_state
    st = load_state(cfg)
    lines = [f"  Morning run:  {cfg.morning_time} on {', '.join(cfg.run_days)}",
             f"  Sync:         " + (f"every {cfg.sync_every_minutes} min between {cfg.sync_from} and "
                                    f"{cfg.sync_until}" if cfg.sync_every_minutes else "off"),
             f"  Installed:    " + ("yes" if installed(cfg) else "no  (install with ./setup.sh --schedule)")
             + (" (launchd)" if is_mac() else " (cron)"),
             f"  Last sync:    {st.get('last_success', 'never')}"
             + (f"  ({st.get('last_result')})" if st.get('last_result') else "")]
    if st.get("failing"):
        lines.append(f"  FAILING since {st.get('failing_since')}: {st.get('last_error')}")
    if is_mac():
        lines.append(f"  Wake the Mac: sudo {wake_command(cfg)}   (./setup.sh --wake)")
    return "\n".join(lines)
