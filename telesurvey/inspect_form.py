"""`telesurvey inspect`: look at real submissions to set up config.json correctly.

Shows which call-result codes, caller IDs and validation statuses the form
actually produces, and whether each is mapped in config.json.
"""
from __future__ import annotations

from collections import Counter

from .kobo import KoboClient, get_field
from .results import caller_of, classify, fetch_since, validation_of


def _similar(keys, word):
    return sorted(k for k in keys if word in k.lower())[:8]


def run(cfg, today: str) -> int:
    since = fetch_since(cfg, today)
    print(f"Submissions on form {cfg.asset_uid} since {since} UTC (lookback {cfg.lookback_days} days)\n")
    recs = KoboClient(cfg).fetch_submissions(since)
    if not recs:
        print("  No submissions in this period. Increase kobo_import.lookback_days to look further back.")
        return 0
    keys = set().union(*(r.keys() for r in recs))
    print(f"  {len(recs)} submissions\n")

    problems = 0
    for label, name in (("phone", cfg.field_phone), ("call result", cfg.field_status),
                        ("caller", cfg.field_surveyor)):
        found = sum(1 for r in recs if get_field(r, name) not in (None, ""))
        mark = "OK " if found else "!! "
        print(f"  {mark}{label:<12} field '{name}': present in {found} of {len(recs)}")
        if not found:
            problems += 1
            hint = _similar(keys, name.split("/")[-1][:5]) or sorted(keys)[:10]
            print(f"       fields that look similar: {', '.join(hint)}")

    print("\n  Validation status" + ("" if cfg.required_validation else
                                     "  (ignored: every submission counts, see kobo_import in config.json)"))
    vals = Counter(validation_of(r, cfg) for r in recs)
    for v, n in vals.most_common():
        print(f"    {n:>6}  {v:<36} -> {classify(cfg, v)}")
    if cfg.required_validation and not vals.get(cfg.required_validation):
        print(f"    (none has the required status '{cfg.required_validation}' yet, so no results will be applied)")

    print("\n  Call-result codes  (field '%s')" % cfg.field_status)
    codes = Counter(str(get_field(r, cfg.field_status) or "").strip().lower() for r in recs)
    for code, n in codes.most_common():
        cat = cfg.category_of(code)
        if cat == "other":
            problems += 1
            print(f"    {n:>6}  {code or '(empty)':<28} -> NOT MAPPED: add it to 'outcomes' in config.json")
        else:
            print(f"    {n:>6}  {code:<28} -> {cat} ({cfg.categories[cat]['action']})")

    print("\n  Callers  (field '%s', falling back to '%s')" % (cfg.field_surveyor, cfg.field_surveyor_fallback))
    known = {s.id for s in cfg.surveyors}
    callers = Counter(caller_of(r, cfg) for r in recs)
    for who, n in callers.most_common():
        status = "in config.json" if who in known else "NOT in config.json surveyors"
        if who not in known:
            problems += 1
        print(f"    {n:>6}  {who or '(empty)':<28} {status}")

    print("\n  " + ("Everything is mapped." if not problems else
                    f"{problems} item(s) above need attention in config.json."))
    return 0 if not problems else 1
