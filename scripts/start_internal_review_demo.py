"""Start the owned historical review demo without seeding or paid model calls."""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
from run_business_package_acceptance import launch, manage, save

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=8393)
    parser.add_argument("--frontend-port", type=int, default=5193)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    package, output = args.package.resolve(), args.output.resolve()
    if not all(p.is_relative_to(ROOT / ".local/acceptance-artifacts") for p in (package, output)) or output.exists():
        parser.error("owned package and new local output required")
    owner = json.loads((package / "environment-owner.json").read_text())
    result = json.loads((package / "result.json").read_text())
    database = owner["database"]
    if owner["run_dir"] != str(package) or result["database"] != database or not re.fullmatch(r"insight_acceptance_\d{8}_v[1-9]\d*", database):
        raise ValueError("package/environment ownership mismatch")
    for port in (args.api_port, args.frontend_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    output.mkdir(parents=True)
    as_of = result["as_of"]
    save(output / "business-clock.json", {"as_of": as_of})
    env = {**os.environ, "PGDATABASE": database, "PGHOST": "127.0.0.1", "DJANGO_DEBUG": "1",
        "DJANGO_SETTINGS_MODULE": "config.settings", "INSIGHT_ACCEPTANCE_ENV": database,
        "INSIGHT_CELERY_NAMESPACE": database, "INSIGHT_STORAGE_ROOT": str(ROOT / ".local/acceptance-storage" / database),
        "INSIGHT_BEAT_SCHEDULE": str(output / f"beat-{database}.db"),
        "INVESTIGATION_PLANNER_PROVIDER": "deterministic", "EVIDENCE_RELEVANCE_ENABLED": "0",
        "INSIGHT_ENABLED_MODEL_PROVIDERS": "", "DEEPSEEK_API_KEY": "",
        "EVIDENCE_CANDIDATE_MODE": "lexical", "CSRF_TRUSTED_ORIGINS": f"http://127.0.0.1:{args.frontend_port}",
        "ACCEPTANCE_API_PROXY": f"http://127.0.0.1:{args.api_port}"}
    services = []
    stopped = False

    def request_stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        manage(env, ["check_acceptance_environment"], output / "check.log")
        services.append(launch(env, as_of, ["manage.py", "runserver", f"127.0.0.1:{args.api_port}", "--noreload"], output / "api.log"))
        services.append(launch(env, as_of, ["celery", "-A", "config.celery", "worker", "-Q", database,
            "--hostname", f"internal-demo-{database}@%h", "--pool=solo", "--concurrency=1", "--loglevel=INFO"], output / "worker.log"))
        services.append(launch(env, as_of, ["celery", "-A", "config.celery", "beat", "--loglevel=INFO"], output / "beat.log"))
        frontend_log = (output / "frontend.log").open("a")
        frontend = subprocess.Popen([shutil.which("pnpm") or "pnpm", "dev", "--port", str(args.frontend_port)],
            cwd=ROOT / "frontend", env=env, stdout=frontend_log, stderr=subprocess.STDOUT, start_new_session=True)
        services.append((frontend, frontend_log))
        with httpx.Client(timeout=2, trust_env=False) as client:
            for _ in range(120):
                if stopped or any(p.poll() is not None for p, _ in services):
                    raise RuntimeError("demo service stopped during startup")
                try:
                    ready = client.get(f"http://127.0.0.1:{args.api_port}/api/v1/health/").status_code == 200
                    ready = ready and client.get(f"http://127.0.0.1:{args.frontend_port}").status_code == 200
                    if ready:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
            else:
                raise TimeoutError("demo startup timeout")
        state = {"status": "ready", "frontend_url": f"http://127.0.0.1:{args.frontend_port}",
            "launcher_pid": os.getpid(),
            "api_url": f"http://127.0.0.1:{args.api_port}", "database": database,
            "credentials_file": str(package / "accounts.json"), "as_of": as_of,
            "real_review_rows": result["real_review_rows"], "paid_calls_enabled": False,
            "intelligence_boundary": "saved functional traces and deterministic runtime; no real model quality pass claim",
            "owned_service_pids": [p.pid for p, _ in services]}
        save(output / "state.json", state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        if args.smoke_only:
            return
        while not stopped:
            if any(p.poll() is not None for p, _ in services):
                raise RuntimeError("owned demo service exited")
            time.sleep(.5)
    finally:
        for process, handle in reversed(services):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            handle.close()
        save(output / "cleanup.json", {"owned_processes_stopped": all(p.poll() is not None for p, _ in services),
            "shared_services_preserved": True, "data_preserved": True})


if __name__ == "__main__":
    main()
