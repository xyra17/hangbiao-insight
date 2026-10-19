"""Serve only this task's screenshot gallery on an unused loopback port."""
from pathlib import Path
import json
import socket
import subprocess
import sys
import time
import urllib.request

root = Path(__file__).resolve().parent
with socket.socket() as probe:
    try:
        probe.bind(('127.0.0.1', 5198))
    except OSError:
        raise SystemExit('Port 5198 is occupied; no existing process was replaced.')
with (root / 'preview-server.log').open('a') as log:
    process = subprocess.Popen(
        [sys.executable, '-m', 'http.server', '5198', '--bind', '127.0.0.1', '--directory', str(root)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
    )
url = 'http://127.0.0.1:5198/index.html'
for attempt in range(20):
    if process.poll() is not None:
        raise SystemExit('The owned preview server could not start.')
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200 and '航标'.encode() in response.read():
                state = {'pid': process.pid, 'url': url, 'root': str(root), 'scope': 'local read-only screenshot gallery; no API proxy'}
                (root / 'preview-service.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(state, ensure_ascii=False))
                break
    except OSError:
        time.sleep(0.1)
else:
    process.terminate()
    raise SystemExit('The owned preview did not become ready.')
