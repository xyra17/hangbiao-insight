"""Stop only process groups recorded by the owned internal demo, including orphans."""

import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def members(groups):
    output = subprocess.check_output(['ps', 'ax', '-o', 'pid=,pgid=,command='], text=True)
    found = []
    for line in output.splitlines():
        row = line.strip().split(None, 2)
        if len(row) == 3 and int(row[1]) in groups:
            found.append((int(row[0]), int(row[1]), row[2]))
    return found


def stop(runtime):
    runtime = runtime.resolve()
    if not runtime.is_relative_to(ROOT / '.local/acceptance-artifacts'):
        raise ValueError('owned runtime directory required')
    state = json.loads((runtime / 'state.json').read_text())
    database = state['database']
    if not re.fullmatch(r'insight_acceptance_\d{8}_v[1-9]\d*', database):
        raise ValueError('owned acceptance database required')
    groups = set(state['owned_service_pids'])
    found = members(groups)
    # Validate every member before signalling any group. Environment values are
    # inspected locally and are never printed, persisted or included in errors.
    for pid, _, _ in found:
        environment = subprocess.check_output(['ps', 'eww', '-p', str(pid), '-o', 'command='], text=True)
        if not re.search(r'(?:^|\s)INSIGHT_CELERY_NAMESPACE=' + re.escape(database) + r'(?=\s|$)', environment):
            raise ValueError('recorded process group no longer belongs to this demo')
    launcher = state['launcher_pid']
    command = subprocess.run(['ps', '-p', str(launcher), '-o', 'command='], capture_output=True, text=True, check=False)
    launcher_owned = command.returncode == 0 and 'start_internal_review_demo.py' in command.stdout and runtime.name in command.stdout
    if command.returncode == 0 and not launcher_owned:
        raise ValueError('recorded launcher PID belongs to another process')
    if launcher_owned:
        os.kill(launcher, signal.SIGTERM)
    active = sorted({group for _, group, _ in found})
    for group in active:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    remaining = []
    for _ in range(50):
        remaining = members(groups)
        if not remaining:
            break
        time.sleep(.2)
    result = {'launcher_signalled': launcher_owned, 'owned_groups_signalled': active,
              'remaining_owned_pids': [pid for pid, _, _ in remaining], 'forced_kill': False}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    result = stop(args.runtime)
    print(json.dumps(result))
    return bool(result['remaining_owned_pids'])


if __name__ == '__main__':
    raise SystemExit(main())
