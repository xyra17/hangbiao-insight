"""Run one acceptance service in its own process group and guarantee shutdown.

The wrapper keeps a drill's server from outliving the test. On SIGTERM it
hard-kills the child's whole process group and exits immediately.

It deliberately does not call ``Popen.wait()`` from the signal handler: waiting
again on the same ``Popen`` while the main thread is already inside ``wait()``
self-deadlocks on CPython's ``_waitpid_lock``. That deadlock was observed on
shared CI runners, where the isolated worker does not converge inside the grace
period and the wrapper stopped responding to further SIGTERM.
"""

import os
import signal
import subprocess
import sys


def main():
    child = subprocess.Popen(sys.argv[1:], start_new_session=True)

    def stop(_signum, _frame):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return child.wait()
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
