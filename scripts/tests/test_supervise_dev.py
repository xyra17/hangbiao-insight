import socket
import os
import signal
import sys
import subprocess
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervise_dev import Service, assert_ports_free, supervise


def test_port_conflict_is_reported_before_children_start():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        try:
            assert_ports_free([port])
        except RuntimeError as exc:
            assert f":{port}" in str(exc)
        else:
            raise AssertionError("occupied port was accepted")


def test_child_exit_fails_and_stops_sibling():
    root = Path(__file__).resolve().parents[2]
    services = [
        Service("long-lived", [sys.executable, "-c", "import time; time.sleep(30)"], root),
        Service("failure", [sys.executable, "-c", "import time; time.sleep(.2); raise SystemExit(7)"], root),
    ]
    started = time.monotonic()
    result = supervise(services, ready_ports=[], stable_seconds=0.05, poll_seconds=0.02)
    assert result == 7
    assert time.monotonic() - started < 5


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # An orphaned child may briefly remain as a zombie before init reaps it.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split()[2] == "Z":
            return
        if sys.platform == "darwin":
            result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
            if result.stdout.strip().startswith("Z"):
                return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} is still alive")


def test_cleanup_kills_grandchild_when_service_leader_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    wrapper = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )
    service = Service("wrapper", [sys.executable, "-c", wrapper, str(pid_file)], tmp_path)
    started = time.monotonic()
    result = supervise([service], ready_ports=[], stable_seconds=0.05, poll_seconds=0.02)
    assert result == 1
    child_pid = int(pid_file.read_text())
    _assert_process_gone(child_pid)
    assert time.monotonic() - started < 5


def test_sigterm_runs_supervisor_cleanup(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = textwrap.dedent(
        """
        import pathlib, subprocess, sys, time
        sys.path.insert(0, sys.argv[1])
        from supervise_dev import Service, supervise
        root = pathlib.Path(sys.argv[1])
        wrapper = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
        )
        raise SystemExit(supervise(
            [Service('wrapper', [sys.executable, '-c', wrapper, sys.argv[2]], root)],
            ready_ports=[], stable_seconds=0.05, poll_seconds=0.02,
        ))
        """
    )
    root = Path(__file__).resolve().parents[1]
    supervisor = subprocess.Popen([sys.executable, "-c", script, str(root), str(pid_file)])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.02)
    assert pid_file.exists(), "service did not start its child"
    child_pid = int(pid_file.read_text())
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=5) == 128 + signal.SIGTERM
    _assert_process_gone(child_pid)
