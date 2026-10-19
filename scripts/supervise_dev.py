"""Start the isolated local stack and fail if any child exits unexpectedly."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Service:
    name: str
    command: list[str]
    cwd: Path


class SupervisorSignal(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def assert_ports_free(ports: list[int]) -> None:
    for port in ports:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Required local dev port 127.0.0.1:{port} is already occupied") from exc


def port_listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def stop_all(processes: list[tuple[Service, subprocess.Popen]], timeout: float = 5.0) -> None:
    # Keep the process-group id even if its leader already exited: grandchildren
    # can outlive wrappers such as uv, celery, or shell scripts.
    for _, process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + timeout
    for _, process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
    # The leader can exit before a grandchild. Escalate by process-group id
    # after the leader grace period so no wrapper descendants are left behind.
    for _, process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for _, process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def supervise(
    services: list[Service],
    *,
    ready_ports: list[int],
    startup_timeout: float = 30.0,
    stable_seconds: float = 1.0,
    poll_seconds: float = 0.1,
) -> int:
    processes: list[tuple[Service, subprocess.Popen]] = []
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum, _frame):
        raise SupervisorSignal(signum)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        assert_ports_free(ready_ports)
        for service in services:
            process = subprocess.Popen(
                service.command,
                cwd=service.cwd,
                env=os.environ.copy(),
                start_new_session=True,
            )
            processes.append((service, process))
        started = time.monotonic()
        ready_since = None
        while True:
            exited = [(service, process.poll()) for service, process in processes if process.poll() is not None]
            if exited:
                service, code = exited[0]
                print(f"Dev service {service.name} exited during startup with status {code}.", file=sys.stderr, flush=True)
                return code or 1
            ready = all(port_listening(port) for port in ready_ports)
            now = time.monotonic()
            ready_since = (ready_since or now) if ready else None
            if ready_since is not None and now - ready_since >= stable_seconds:
                print("Local API, frontend, worker, and beat are running; press Ctrl+C to stop.", flush=True)
                break
            if now - started >= startup_timeout:
                missing = [str(port) for port in ready_ports if not port_listening(port)]
                print(f"Dev services did not become ready on required ports: {', '.join(missing)}", file=sys.stderr, flush=True)
                return 1
            time.sleep(poll_seconds)

        while True:
            for service, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"Dev service {service.name} exited unexpectedly with status {code}.", file=sys.stderr, flush=True)
                    return code or 1
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return 0
    except SupervisorSignal as exc:
        return 128 + exc.signum
    except Exception as exc:
        print(f"Unable to start local dev services: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_all(processes)
        signal.signal(signal.SIGTERM, previous_sigterm)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--ready-port", type=int, action="append", default=[])
    args = parser.parse_args()
    root = args.repo_root.resolve()
    uv = os.getenv("UV_EXECUTABLE", "uv")
    backend = root / "backend"
    services = [
        Service("api", [uv, "run", "--env-file", "../.env.local", "python", "manage.py", "runserver", "127.0.0.1:8280", "--noreload"], backend),
        Service("worker", [uv, "run", "--env-file", "../.env.local", "celery", "-A", "config", "worker", "--loglevel=INFO", "--concurrency=2"], backend),
        Service("beat", [uv, "run", "--env-file", "../.env.local", "celery", "-A", "config", "beat", "--loglevel=INFO"], backend),
        Service("frontend", ["pnpm", "dev", "--", "--host", "127.0.0.1", "--strictPort"], root / "frontend"),
    ]
    return supervise(services, ready_ports=args.ready_port)


if __name__ == "__main__":
    raise SystemExit(main())
