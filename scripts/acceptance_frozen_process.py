#!/usr/bin/env python3
"""Freeze Django business time only in a guarded, isolated acceptance process.

OS time/monotonic and the runner's execution timestamps remain real. Never a
production setting: this wrapper requires the same strict acceptance guard.
"""
import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--clock-file", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    date.fromisoformat(args.as_of)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    import django
    django.setup()
    from insight.management.commands._acceptance import (
        require_isolated_acceptance_environment,
    )
    require_isolated_acceptance_environment()
    from django.utils import timezone
    if args.clock_file and not args.clock_file.resolve().is_relative_to(Path(__file__).resolve().parents[1] / ".local/acceptance-artifacts"):
        parser.error("clock-file must be owned local acceptance artifact")
    def business_now():
        clock = json.loads(args.clock_file.read_text()) if args.clock_file else {"as_of": args.as_of}
        instant = datetime.combine(date.fromisoformat(clock["as_of"]), datetime.min.time(), ZoneInfo("America/Los_Angeles")).replace(hour=13)
        anchor = clock.get("anchor_real_utc", clock.get("changed_at_real_utc"))
        elapsed = max(0, (datetime.now(UTC) - datetime.fromisoformat(anchor)).total_seconds()) if anchor else 0
        return instant + timedelta(seconds=elapsed)
    timezone.now = business_now
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or command[0] not in {"manage.py", "celery"}:
        parser.error("command must begin with manage.py or celery")
    sys.argv = command
    if command[0] == "manage.py":
        from django.core.management import execute_from_command_line
        execute_from_command_line(command)
    else:
        from celery.bin.celery import main as celery_main
        return celery_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
