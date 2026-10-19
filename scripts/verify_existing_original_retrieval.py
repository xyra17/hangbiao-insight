"""Verify current retrieval over an explicitly owned historical import, without reimporting."""
import argparse
import csv
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from release_acceptance import ROOT, digest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    package, output = args.package_dir.resolve(), args.output.resolve()
    if not all(path.is_relative_to(ROOT / '.local/acceptance-artifacts') for path in (package, output)) or package == output:
        parser.error('owned input and separate local output required')
    owner_path = package / 'environment-owner.json'
    owner = json.loads(owner_path.read_text())
    if not owner_path.is_relative_to(Path(owner['run_dir'])) or not re.fullmatch(r'insight_acceptance_\d{8}_v[1-9]\d*', owner['database']):
        parser.error('unbound database ownership')
    lineage = json.loads((package / 'originals-lineage.json').read_text())
    original = ROOT / lineage['source_path']
    assert digest(original) == lineage['source_sha256'], 'original source changed'
    previous = json.loads((package / 'originals-retrieval.json').read_text())
    selected = {row['record_id'] for row in lineage['original_row_references']}
    with original.open(encoding='utf-8-sig', newline='') as stream:
        rows = {row['review_id']: row for row in csv.DictReader(stream) if row['review_id'] in selected}
    assert len(rows) == len(selected) == 600
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'PGDATABASE': owner['database'], 'PGHOST': '127.0.0.1', 'PGPORT': '55432',
           'INSIGHT_ACCEPTANCE_ENV': owner['database'], 'INSIGHT_CELERY_NAMESPACE': owner['database'],
           'INSIGHT_STORAGE_ROOT': str(ROOT / '.local/acceptance-storage' / owner['database']),
           'INSIGHT_BEAT_SCHEDULE': str(output / ('beat-' + owner['database'] + '.db')),
           'DJANGO_SETTINGS_MODULE': 'config.settings', 'DJANGO_DEBUG': '1',
           'INVESTIGATION_PLANNER_PROVIDER': 'deterministic', 'EVIDENCE_RELEVANCE_ENABLED': '0'}
    command = [sys.executable, str(ROOT / 'scripts/acceptance_frozen_process.py'), '--as-of', '2023-09-03',
               '--', 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload']
    with (output / 'api.log').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT / 'backend', env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            session = requests.Session()
            session.trust_env = False
            base = f'http://127.0.0.1:{port}/api/v1'
            for _ in range(100):
                try:
                    if session.get(base + '/health/', timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                if process.poll() is not None:
                    raise RuntimeError('owned API startup failed')
                time.sleep(.2)
            account = next(row for row in json.loads((package / 'accounts.json').read_text()) if row['username'] == 'accept_admin')
            token = session.get(base + '/session/', timeout=10).json()['csrf_token']
            login = session.post(base + '/login/', json=account, headers={'X-CSRFToken': token}, timeout=10)
            assert login.status_code == 200
            scope = previous['scope']
            assert scope['shop'] == json.loads((package / 'identities.json').read_text())['shop_id']
            response = session.post(base + '/evidence-search/', json={'shop': scope['shop'], 'child_asin': scope['asin'],
                    'query': previous['query'], 'start': scope['start'], 'end': scope['end'], 'kind': 'reviews'},
                    headers={'X-CSRFToken': login.json()['csrf_token']}, timeout=30)
            assert response.status_code == 200
            result = response.json()
            assert result['items'] and result['match_status'] == 'relevant_hits'
            assert result['source_manifest'] == previous['source_manifest'], 'frozen source versions changed'
            assert all(row['record_id'] in selected and row['text'] == rows[row['record_id']]['text']
                       and row['title'] == rows[row['record_id']]['title'] and row['fact_id'] and row['raw_row_id'] for row in result['items'])
            assert digest(original) == lineage['source_sha256']
            save(output / 'retrieval.json', result)
            save(output / 'result.json', {'status': 'passed', 'database': owner['database'], 'historical_selected_rows': 600,
                 'current_retrieval_version': result['retrieval_version'], 'current_hit_count': len(result['items']),
                 'source_sha256': lineage['source_sha256'], 'no_reimport': True, 'no_business_artifact_rewrite': True,
                 'scope': 'current actual API original-row citation check; not all-corpus recall or independent semantic truth'})
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            save(output / 'cleanup.json', {'owned_api_stopped': process.poll() is not None, 'shared_services_preserved': True})


if __name__ == '__main__':
    main()
