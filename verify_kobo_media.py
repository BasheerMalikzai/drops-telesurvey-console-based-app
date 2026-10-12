#!/usr/bin/env python3
"""Check that assignments.csv made it to the KoBoToolbox form and works as a form attachment.

Uses the KoBo and database settings in .env / config.json, so there is nothing to configure.

    .venv/bin/python verify_kobo_media.py                  # all checks
    .venv/bin/python verify_kobo_media.py --save-copy kobo_copy.csv
    .venv/bin/python verify_kobo_media.py --skip-phones    # skip the KoboCollect check
    .venv/bin/python verify_kobo_media.py --help

Exit code 0 = attachment OK (possibly with warnings), 1 = a check failed.
Same as: .venv/bin/python -m telesurvey verify
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telesurvey.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["verify"] + sys.argv[1:]))
