"""Run one acceptance service in its own process group and forward shutdown."""

import os
import signal
import subprocess
import sys


def main():
    child = subprocess.Popen(sys.argv[1:], start_new_session=True)

    def stop(signum, _frame):
        try:
            os.killpg(child.pid, signum)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return child.wait()
    finally:
        stop(signal.SIGTERM, None)


if __name__ == "__main__":
    raise SystemExit(main())
