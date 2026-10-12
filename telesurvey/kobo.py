"""Talking to the KoBoToolbox API: download submissions, upload the assignment file."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def get_field(record: Dict[str, Any], name: str):
    """Find a field even when it sits inside a KoBo group ('group/name')."""
    if not name:
        return None
    if name in record:
        return record[name]
    for key, value in record.items():
        if key.endswith("/" + name):
            return value
    return None


class KoboClient:
    def __init__(self, cfg):
        import requests  # imported here so offline runs don't need it
        self.cfg = cfg
        self.api = cfg.api_base
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {cfg.kobo_token}"

    def _get(self, url, **params):
        r = self.session.get(url, params=params or None, timeout=90)
        r.raise_for_status()
        return r.json()

    def asset(self) -> Dict[str, Any]:
        return self._get(f"{self.api}/", format="json")

    def fetch_submissions(self, since_utc: str) -> List[Dict[str, Any]]:
        """All submissions made since since_utc (any validation status: the caller decides).

        Re-reading a window, instead of 'everything after the last id', is what lets
        approvals that happen days after the call still be picked up."""
        url = f"{self.api}/data/"
        params = {"format": "json", "limit": 1000,
                  "query": json.dumps({"_submission_time": {"$gte": since_utc}}),
                  "sort": json.dumps({"_id": 1})}
        out: List[Dict[str, Any]] = []
        while url:
            data = self._get(url, **params) if params else self._get(url)
            out += data.get("results", [])
            url, params = data.get("next"), None
        # in case the server ignored the query, filter here as well
        out = [r for r in out if str(r.get("_submission_time", "9999")) >= since_utc]
        log.info("Fetched %d submissions from KoBo (since %s UTC)", len(out), since_utc)
        return out

    def upload_media(self, path: Path, today: str) -> None:
        fname = self.cfg.media_filename
        files = self._get(f"{self.api}/files/", file_type="form_media", format="json")
        for item in files.get("results", []):
            if (item.get("metadata") or {}).get("filename") == fname:
                self.session.delete(f"{self.api}/files/{item['uid']}/", timeout=60).raise_for_status()

        with open(path, "rb") as f:
            r = self.session.post(
                f"{self.api}/files/", timeout=120,
                data={"file_type": "form_media", "description": f"Assignments {today}",
                      "metadata": json.dumps({"filename": fname})},
                files={"content": (fname, f, "text/csv")})
        r.raise_for_status()
        log.info("Uploaded %s to KoBo form %s", fname, self.cfg.asset_uid)

        if self.cfg.redeploy:
            self.redeploy()

    def redeploy(self) -> None:
        version = self.asset().get("version_id")
        r = self.session.patch(f"{self.api}/deployment/", timeout=120,
                               json={"active": True, "version_id": version})
        if r.ok:
            log.info("Form redeployed")
        else:
            log.warning("Redeploy returned %s: %s (redeploy manually in KoBo if devices "
                        "don't show today's list)", r.status_code, r.text[:200])

    def check(self) -> List[str]:
        """Connection and form sanity check. Returns a list of problems (empty = OK)."""
        problems = []
        a = self.asset()
        print(f"  Form:        {a.get('name')}")
        print(f"  Deployed:    {'yes' if a.get('deployment__active') else 'no'}")
        print(f"  Submissions: {a.get('deployment__submission_count', '?')}")
        if not a.get("deployment__active"):
            problems.append("The form is not deployed")

        names = {q.get("name") or q.get("$autoname") for q in (a.get("content") or {}).get("survey", [])}
        for label, fname in (("phone", self.cfg.field_phone), ("call status", self.cfg.field_status),
                             ("caller", self.cfg.field_surveyor)):
            # config may give a group path ("recording_group/status_call"); the form lists "status_call"
            if fname.split("/")[-1] not in names:
                problems.append(f"Form has no question named '{fname}' ({label} field in config.json)")
        return problems
