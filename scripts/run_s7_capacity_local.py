"""Launch the S7 runner with host-side DB and Redis endpoints for the isolated Compose project."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPOSITORY_ROOT / "dataset" / "run_capacity_acceptance.py"


def main() -> int:
    redis_password = os.environ.get("INSIGHT_REDIS_PASSWORD")
    if not redis_password:
        raise SystemExit("INSIGHT_REDIS_PASSWORD is required from the local, untracked environment file.")
    os.environ["PGHOST"] = "127.0.0.1"
    os.environ["PGPORT"] = "56432"
    os.environ["CELERY_BROKER_URL"] = (
        f"redis://:{quote(redis_password, safe='')}@127.0.0.1:57379/0"
    )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    sys.argv = [str(RUNNER), *sys.argv[1:]]
    runpy.run_path(str(RUNNER), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
