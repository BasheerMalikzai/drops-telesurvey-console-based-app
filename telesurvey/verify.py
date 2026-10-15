"""Check that assignments.csv reached the KoBo form and works as a form attachment.

Run with:  .venv/bin/python -m telesurvey verify      (or: python verify_kobo_media.py)

Each check prints PASS, WARN, FAIL or INFO. The exit code is 1 if anything FAILed.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import os
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Optional

REQUIRED_COLUMNS = ["name", "label", "surveyor_id"]
OPENROSA_HEADERS = {"X-OpenRosa-Version": "1.0"}
NS_FORMLIST = "{http://openrosa.org/xforms/xformsList}"
NS_MANIFEST = "{http://openrosa.org/xforms/xformsManifest}"


class Report:
    def __init__(self):
        self.counts = Counter()

    def _line(self, level: str, title: str, detail: str = "") -> None:
        self.counts[level] += 1
        print(f"  [{level}] {title}")
        if detail:
            for d in str(detail).splitlines():
                print(f"         {d}")

    def ok(self, t, d=""):   self._line("PASS", t, d)
    def warn(self, t, d=""): self._line("WARN", t, d)
    def fail(self, t, d=""): self._line("FAIL", t, d)
    def info(self, t, d=""): self._line("INFO", t, d)

    @staticmethod
    def section(title: str) -> None:
        print(f"\n{title}")

    def summary(self) -> int:
        c = self.counts
        print(f"\nResult: {c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} failed")
        if c["FAIL"]:
            print("The attachment is NOT ready for surveyors. Fix the FAIL items above.")
            return 1
        if c["WARN"]:
            print("The attachment works, but check the warnings above.")
        else:
            print("The attachment is on KoBo and ready for surveyors.")
        return 0


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def strip_hash(value: Optional[str]) -> str:
    """KoBo writes hashes as 'md5:abc…'."""
    return (value or "").split(":", 1)[-1].strip().lower()


# --------------------------------------------------------------------------- checks

def check_form(rep: Report, cfg, s) -> Optional[dict]:
    rep.section("1. Form")
    try:
        r = s.get(f"{cfg.api_base}/", params={"format": "json"}, timeout=60)
    except Exception as e:
        rep.fail("Could not reach KoBo", f"{cfg.kobo_url}: {e}")
        return None
    if r.status_code in (401, 403):
        rep.fail("KoBo rejected the API token", "Check KOBO_TOKEN in .env")
        return None
    if r.status_code == 404:
        rep.fail("Form not found", f"No form with UID '{cfg.asset_uid}' (KOBO_ASSET_UID in .env)")
        return None
    r.raise_for_status()
    asset = r.json()
    rep.ok("Form found", f"'{asset.get('name')}' ({cfg.asset_uid})")
    active = asset.get("deployment__active")
    if active is None:
        active = asset.get("has_deployment")
    if active:
        rep.ok("Form is deployed")
    else:
        rep.fail("Form is not deployed", "Deploy it in KoBo; phones cannot download an undeployed form")
    return asset


def check_form_design(rep: Report, cfg, asset: dict) -> None:
    rep.section("2. Form design")
    fname = cfg.media_filename
    stem = fname.rsplit(".", 1)[0]
    survey = (asset.get("content") or {}).get("survey", [])
    searchers = [q for q in survey if re.search(r"""search\(\s*['"]%s['"]""" % re.escape(stem),
                                                str(q.get("appearance", "")))]
    if searchers:
        _check_search(rep, cfg, searchers)
        return
    users = [q for q in survey if fname in json.dumps(q)]
    if not users:
        rep.fail(f"No question uses {fname}",
                 f"Add a question of type: select_one_from_file {fname}\n"
                 f"or the appearance search('{stem}', ...)\n"
                 f"(or set KOBO_MEDIA_FILENAME in .env to the file name the form uses)")
        return
    q = users[0]
    qname = q.get("name") or q.get("$autoname") or "?"
    rep.ok(f"Question '{qname}' uses {fname}")
    if qname != cfg.field_phone.split("/")[-1]:
        rep.warn(f"That question is named '{qname}', but config.json expects '{cfg.field_phone}'",
                 "Rename the question or change kobo_fields.phone in config.json")
    cf = q.get("choice_filter", "")
    if cfg.field_surveyor in cf:
        rep.ok("Numbers are filtered per surveyor", f"choice_filter: {cf}")
    else:
        rep.warn("No choice_filter on the surveyor ID",
                 f"Every surveyor would see everyone's numbers. Set:\n"
                 f"choice_filter: {cfg.field_surveyor}=${{{cfg.field_surveyor}}}")


def find_attachment(rep: Report, cfg, s) -> Optional[dict]:
    rep.section("3. Attachment on KoBo")
    fname = cfg.media_filename
    r = s.get(f"{cfg.api_base}/files/", params={"file_type": "form_media", "format": "json"}, timeout=60)
    r.raise_for_status()
    files = r.json().get("results", [])
    matches = [f for f in files if (f.get("metadata") or {}).get("filename") == fname]
    others = sorted({(f.get("metadata") or {}).get("filename", "?") for f in files} - {fname})
    if not matches:
        rep.fail(f"{fname} is not attached to the form",
                 "Run ./daily.sh (or: .venv/bin/python -m telesurvey refresh) to upload it"
                 + (f"\nOther media on the form: {', '.join(others)}" if others else ""))
        return None
    matches.sort(key=lambda f: f.get("date_created") or "")
    att = matches[-1]
    rep.ok(f"{fname} is attached", f"uploaded {att.get('date_created', '?')}")
    if len(matches) > 1:
        rep.warn(f"{len(matches)} copies of {fname} are attached",
                 "Phones may get any of them. Delete the older ones in KoBo > form > Settings > Media,\n"
                 "or run ./daily.sh, which replaces all copies with one")
    if others:
        rep.info("Other media files on the form", ", ".join(others))
    return att


def download_attachment(rep: Report, cfg, s, att: dict, save_copy: Optional[Path]) -> Optional[bytes]:
    url = att.get("content") or f"{cfg.api_base}/files/{att.get('uid')}/content/"
    try:
        r = s.get(url, timeout=120)
        r.raise_for_status()
    except Exception as e:
        rep.fail("Could not download the attachment from KoBo", str(e)[:300])
        return None
    data = r.content
    rep.ok("Attachment downloads", f"{len(data):,} bytes, md5 {md5(data)}")
    recorded = strip_hash((att.get("metadata") or {}).get("hash"))
    if recorded:
        if recorded == md5(data):
            rep.ok("Download matches the checksum KoBo recorded")
        else:
            rep.warn("Download does not match KoBo's recorded checksum",
                     f"recorded {recorded}, downloaded {md5(data)}; re-upload with ./daily.sh")
    if save_copy:
        save_copy.write_bytes(data)
        rep.info("Saved KoBo's copy", str(save_copy))
    return data


def check_contents(rep: Report, cfg, data: bytes, day: str) -> Optional[list]:
    rep.section("4. File contents")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        rep.fail("File is not UTF-8 text", "KoBo needs a UTF-8 CSV")
        return None
    reader = csv.DictReader(io.StringIO(text))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        rep.fail("Required columns missing", f"missing: {', '.join(missing)}; found: {', '.join(cols) or 'none'}")
        return None
    rep.ok("Columns are correct", ", ".join(cols))

    rows = list(reader)
    if not rows:
        rep.warn("The file has no numbers",
                 "Normal late in the day when every number has a result; otherwise check the morning run")
        return rows
    rep.ok(f"{len(rows)} numbers in the file")

    names = [(r.get("name") or "").strip() for r in rows]
    empty = sum(1 for n in names if not n)
    spaced = [n for n in names if " " in n]
    dupes = [n for n, k in Counter(names).items() if n and k > 1]
    if empty:
        rep.fail(f"{empty} rows have an empty 'name'", "Every choice needs a name (the phone number)")
    if spaced:
        rep.fail(f"{len(spaced)} names contain spaces", f"e.g. {spaced[0]!r}; choice names cannot contain spaces")
    if dupes:
        rep.fail(f"{len(dupes)} phone numbers appear more than once", f"e.g. {dupes[0]}")
    if not (empty or spaced or dupes):
        rep.ok("Every row has a unique, valid choice name")

    no_label = sum(1 for r in rows if not (r.get("label") or "").strip())
    if no_label:
        rep.warn(f"{no_label} rows have no label", "Surveyors would see blank options")

    per = Counter((r.get("surveyor_id") or "").strip() for r in rows)
    active = {sv.id for sv in cfg.active_surveyors()}
    unknown = sorted(k for k in per if k not in active)
    if unknown:
        rep.warn("Rows for surveyor IDs that are not active in config.json",
                 f"{', '.join(unknown or ['(blank)'])}: nobody can pick those numbers")
    else:
        rep.ok("All rows belong to active surveyors")
    idle = sorted(active - set(per))
    rep.info("Numbers per surveyor", ", ".join(f"{k}: {v}" for k, v in sorted(per.items()))
             + (f"\nNo numbers for: {', '.join(idle)}" if idle else ""))

    dates = Counter((r.get("assign_date") or "").strip() for r in rows)
    if "assign_date" in cols:
        if set(dates) == {day}:
            rep.ok(f"File is for today ({day})")
        else:
            rep.warn("File is not for today",
                     f"dates in file: {', '.join(sorted(dates))}; today is {day}.\n"
                     "The morning run may not have happened or its upload failed")
    return rows


def compare_local(rep: Report, cfg, data: bytes, local: Path) -> None:
    rep.section("5. Compared with the copy generated here")
    if not local.exists():
        rep.info("No local copy to compare", f"{local} not found (normal on a different computer)")
        return
    mine = local.read_bytes()
    if md5(mine) == md5(data):
        rep.ok("KoBo has exactly the file generated here", str(local))
    else:
        age = dt.datetime.fromtimestamp(local.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        rep.warn("KoBo's file differs from the local one",
                 f"local {local} (written {age}).\n"
                 "The latest upload may have failed or not happened yet; run: .venv/bin/python -m telesurvey refresh")


def compare_database(rep: Report, cfg, rows: list, day: str) -> None:
    rep.section("6. Compared with the database")
    from . import db
    try:
        conn = db.connect(cfg, create_schema=False)
    except Exception as e:
        rep.info("Database not checked", str(e)[:200])
        return
    try:
        recs = conn.execute("SELECT phone, called FROM assignments WHERE assign_date=?", (day,)).fetchall()
    finally:
        conn.close()
    if not recs:
        rep.warn(f"No assignment in the database for {day}", "Has today's morning run happened?")
        return
    to_call = {r["phone"] for r in recs if not r["called"]}
    finished = {r["phone"] for r in recs if r["called"]}
    in_file = {(r.get("name") or "").strip() for r in rows}
    missing = to_call - in_file
    stale = in_file & finished
    foreign = in_file - to_call - finished
    if missing:
        rep.warn(f"{len(missing)} numbers still to call are missing from KoBo's file",
                 "Surveyors can't reach them. Upload again: .venv/bin/python -m telesurvey refresh")
    else:
        rep.ok(f"All {len(to_call)} numbers still to call are in the file")
    if stale:
        rep.info(f"{len(stale)} numbers in the file already have a result",
                 "They drop out at the next sync (or tomorrow)")
    if foreign:
        rep.warn(f"{len(foreign)} numbers in the file are not in today's assignment",
                 "KoBo has an old file; run ./daily.sh")


def _check_search(rep: Report, cfg, questions) -> None:
    """Questions using the older search('Assignment', ...) appearance (KoboCollect only)."""
    from .export import columns
    COLUMNS = columns(cfg)
    caller = cfg.field_surveyor.split("/")[-1]
    filtered = False
    for q in questions:
        qname = q.get("name") or q.get("$autoname") or "?"
        app = str(q.get("appearance", ""))
        rep.ok(f"Question '{qname}' uses {cfg.media_filename} through search()", f"appearance: {app}")
        m = re.search(r"""search\(\s*['"][^'"]+['"]\s*,\s*['"](\w+)['"]\s*,\s*['"]([^'"]+)['"]\s*,"""
                      r"""\s*\$\{(\w+)\}""", app)
        if not m:
            continue
        how, column, ref = m.groups()
        if column not in COLUMNS:
            rep.fail(f"'{qname}' searches column '{column}', which {cfg.media_filename} doesn't have",
                     "Columns available: " + ", ".join(COLUMNS))
        if ref == caller:
            filtered = filtered or column in ("assigned_to", "surveyor_id")
            if how == "contains":
                rep.warn(f"'{qname}' matches the caller ID with 'contains'",
                         f"ID 232 would also see 2323's numbers. Use: "
                         f"search('{cfg.media_filename.rsplit('.', 1)[0]}', 'matches', '{column}', ${{{ref}}})")
    if filtered:
        rep.ok("Numbers are filtered per surveyor")
    else:
        rep.warn("No search() filters on the caller ID",
                 f"Every caller would see everyone's numbers. Use, e.g.:\n"
                 f"search('{cfg.media_filename.rsplit('.', 1)[0]}', 'matches', 'assigned_to', ${{{caller}}})")
    rep.info("search() works in KoboCollect only, not in web forms (Enketo)")


def check_phone_view(rep: Report, cfg, s, asset: dict, data: bytes) -> None:
    """What KoboCollect actually downloads: the OpenRosa form list and manifest."""
    rep.section("7. What phones receive (KoboCollect)")
    import requests

    ident = asset.get("deployment__identifier") or ""
    if "/forms/" in ident:
        base = ident.split("/forms/")[0]
    else:
        owner = asset.get("owner__username") or ""
        base = cfg.kobo_url.replace("://kf.", "://kc.").rstrip("/") + (f"/{owner}" if owner else "")

    auths = [("API token", None)]
    if os.environ.get("KOBO_USERNAME") and os.environ.get("KOBO_PASSWORD"):
        auths.append(("username/password", (os.environ["KOBO_USERNAME"], os.environ["KOBO_PASSWORD"])))

    form_list = None
    for label, basic in auths:
        try:
            if basic:
                r = requests.get(f"{base}/formList", headers=OPENROSA_HEADERS, auth=basic, timeout=60)
            else:
                r = s.get(f"{base}/formList", headers=OPENROSA_HEADERS, timeout=60)
            if r.ok and b"xforms" in r.content:
                form_list = r.content
                break
        except Exception:
            continue
    if form_list is None:
        rep.warn("Could not read the form list that phones use",
                 f"{base}/formList did not accept the API token.\n"
                 "Optional: add KOBO_USERNAME and KOBO_PASSWORD to .env to enable this check")
        return

    try:
        root = ET.fromstring(form_list)
    except ET.ParseError:
        rep.warn("Form list could not be parsed")
        return
    entry = None
    for xf in root.iter(f"{NS_FORMLIST}xform"):
        if (xf.findtext(f"{NS_FORMLIST}formID") or "").strip() == cfg.asset_uid:
            entry = xf
    if entry is None:
        rep.fail("Phones don't see this form", "It is missing from the form list: is it deployed and shared?")
        return
    manifest_url = (entry.findtext(f"{NS_FORMLIST}manifestUrl") or "").strip()
    if not manifest_url:
        rep.fail("Phones see the form but no media manifest", "Redeploy the form in KoBo")
        return
    rep.ok("Phones see the form")

    try:
        r = s.get(manifest_url, headers=OPENROSA_HEADERS, timeout=60)
        if not r.ok and len(auths) > 1:
            r = requests.get(manifest_url, headers=OPENROSA_HEADERS, auth=auths[1][1], timeout=60)
        r.raise_for_status()
        mroot = ET.fromstring(r.content)
    except Exception as e:
        rep.warn("Could not read the media manifest", str(e)[:200])
        return
    media = None
    for mf in mroot.iter(f"{NS_MANIFEST}mediaFile"):
        if (mf.findtext(f"{NS_MANIFEST}filename") or "").strip() == cfg.media_filename:
            media = mf
    if media is None:
        rep.fail(f"Phones are not offered {cfg.media_filename}",
                 "It is attached but not in the deployed form's manifest. Redeploy the form in KoBo")
        return
    served_hash = strip_hash(media.findtext(f"{NS_MANIFEST}hash"))
    if served_hash == md5(data):
        rep.ok(f"Phones are offered the current {cfg.media_filename}", f"md5 {served_hash}")
    else:
        rep.fail(f"Phones are offered a different {cfg.media_filename}",
                 f"manifest md5 {served_hash or '?'} vs attachment md5 {md5(data)}.\n"
                 "Redeploy the form (KOBO_REDEPLOY=1 in .env does this after every upload)")
        return
    dl = (media.findtext(f"{NS_MANIFEST}downloadUrl") or "").strip()
    if dl:
        try:
            r = s.get(dl, timeout=120)
            if not r.ok and len(auths) > 1:
                r = requests.get(dl, auth=auths[1][1], timeout=120)
            r.raise_for_status()
            if md5(r.content) == md5(data):
                rep.ok("The file phones download is identical")
            else:
                rep.fail("The file phones download is different", "Redeploy the form in KoBo")
        except Exception as e:
            rep.warn("Could not download the file the way phones do", str(e)[:200])
    rep.info("Surveyors still need to refresh the form in KoboCollect to get it")


# --------------------------------------------------------------------------- entry

def run(cfg, day: Optional[str] = None, local: Optional[Path] = None, save_copy: Optional[Path] = None,
        check_db: bool = True, check_phones: bool = True) -> int:
    import requests

    day = day or dt.date.today().isoformat()
    print(f"Checking {cfg.media_filename} on KoBo form {cfg.asset_uid or '(not set)'} ({cfg.kobo_url})")
    rep = Report()
    if not cfg.kobo_enabled:
        rep.fail("KoBo is not configured", "Set KOBO_TOKEN and KOBO_ASSET_UID in .env")
        return rep.summary()

    s = requests.Session()
    s.headers["Authorization"] = f"Token {cfg.kobo_token}"

    asset = check_form(rep, cfg, s)
    if asset is None:
        return rep.summary()
    check_form_design(rep, cfg, asset)
    att = find_attachment(rep, cfg, s)
    if att is None:
        return rep.summary()
    data = download_attachment(rep, cfg, s, att, save_copy)
    if data is None:
        return rep.summary()
    rows = check_contents(rep, cfg, data, day)
    compare_local(rep, cfg, data, local or (cfg.output_dir / cfg.media_filename))
    if check_db and rows is not None:
        compare_database(rep, cfg, rows, day)
    if check_phones:
        check_phone_view(rep, cfg, s, asset, data)
    return rep.summary()
