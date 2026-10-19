"""Interrupt a dedicated beat and Redis; preserve the scheduler file and routing."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import redis

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ownership-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    owner_path, output = args.ownership_file.resolve(), args.output.resolve()
    if not owner_path.is_relative_to(ROOT / ".local/acceptance-artifacts") or not output.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        parser.error("explicit local ownership required")
    owner = json.loads(owner_path.read_text())
    database = owner["database"]
    if not owner_path.is_relative_to(Path(owner["run_dir"])):
        parser.error("ownership does not bind this run")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    broker_process = subprocess.Popen([shutil.which("redis-server"), "--bind", "127.0.0.1", "--port", str(port), "--save", "", "--appendonly", "no", "--dir", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    beat = None
    broker = redis.Redis(host="127.0.0.1", port=port)
    schedule = output / f"beat-{database}.db"
    env = {**os.environ, "PGDATABASE": database, "PGHOST": "127.0.0.1", "PGPORT": "55432", "DJANGO_DEBUG": "1", "INSIGHT_ACCEPTANCE_ENV": database, "INSIGHT_CELERY_NAMESPACE": database, "CELERY_BROKER_URL": f"redis://127.0.0.1:{port}/0", "INSIGHT_OUTBOX_DISPATCH_INTERVAL_SECONDS": "1", "INSIGHT_BEAT_SCHEDULE": str(schedule), "INSIGHT_STORAGE_ROOT": str(ROOT/".local/acceptance-storage"/database)}
    queue = database+":"+database

    def wait(check):
        for _ in range(150):
            try:
                if check():
                    return
            except redis.ConnectionError:
                pass
            time.sleep(.1)
        raise RuntimeError("isolated scheduler did not reach expected state")

    def launch(log):
        return subprocess.Popen(["uv", "run", "--directory", str(ROOT/"backend"), "--env-file", "../.env.local", "celery", "-A", "config.celery", "beat", "--max-interval", "1", "--schedule", str(schedule), "--loglevel", "INFO"], env=env, stdout=log, stderr=subprocess.STDOUT)

    try:
        wait(broker.ping)
        with (output/"beat.log").open("w") as log:
            beat = launch(log)
            wait(lambda: broker.llen(queue) >= 2)
            beat.terminate()
            beat.wait(timeout=10)
            count = broker.llen(queue)
            assert schedule.exists()
            time.sleep(1.2)
            assert broker.llen(queue) == count
            beat = launch(log)
            wait(lambda: broker.llen(queue) > count)
            resumed = broker.llen(queue)
            beat.terminate()
            beat.wait(timeout=10)
            assert not broker.exists("celery")
        (output/"result.json").write_text(json.dumps({"status": "passed", "beat_interrupted": True, "scheduler_file_preserved": True, "queued_before_restart": count, "queued_after_restart": resumed, "default_queue_empty": True, "scope": "real dedicated beat restart and prefixed broker scheduling; business schedule/job idempotency separately covered in backend recovery suite", "shared_redis_touched": False}, indent=2)+"\n")
    finally:
        if beat and beat.poll() is None:
            beat.terminate()
            beat.wait(timeout=10)
        broker_process.terminate()
        broker_process.wait(timeout=10)
        (output/"cleanup.json").write_text(json.dumps({"beat_stopped": not beat or beat.poll() is not None, "owned_redis_stopped": broker_process.poll() is not None, "shared_services_untouched": True})+"\n")


if __name__ == "__main__":
    main()
