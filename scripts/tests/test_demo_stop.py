"""Controlled orphan shutdown checks; no real process receives a signal."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('demo_stop', Path(__file__).resolve().parents[1] / 'stop_internal_review_demo.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    path = tmp_path / '.local/acceptance-artifacts/demo'
    path.mkdir(parents=True)
    (path / 'state.json').write_text(json.dumps({'database': 'insight_acceptance_20261013_v1234',
                                               'owned_service_pids': [100], 'launcher_pid': 200}))
    signals = []
    monkeypatch.setattr(module.os, 'killpg', lambda pid, sig: signals.append(('group', pid)))
    monkeypatch.setattr(module.os, 'kill', lambda pid, sig: signals.append(('pid', pid)))
    monkeypatch.setattr(module.time, 'sleep', lambda delay: None)
    return path, signals


@pytest.mark.parametrize('namespace', ['different', 'insight_acceptance_20261013_v12345'])
def test_foreign_namespace_or_prefix_collision_sends_no_signal(runtime, monkeypatch, namespace):
    path, signals = runtime
    monkeypatch.setattr(module, 'members', lambda groups: [(101, 100, 'fixture')])
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: 'INSIGHT_CELERY_NAMESPACE=' + namespace)
    with pytest.raises(ValueError, match='no longer belongs'):
        module.stop(path)
    assert signals == []


def test_orphan_children_are_stopped_after_all_members_verified(runtime, monkeypatch):
    path, signals = runtime
    calls = []
    def members(groups):
        return [] if signals else [(101, 100, 'fixture')]
    monkeypatch.setattr(module, 'members', members)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: calls.append(a) or 'INSIGHT_CELERY_NAMESPACE=insight_acceptance_20261013_v1234 ')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    result = module.stop(path)
    assert calls and signals == [('group', 100)]
    assert result['remaining_owned_pids'] == [] and not result['launcher_signalled']


def test_reused_launcher_pid_is_not_stopped(runtime, monkeypatch):
    path, signals = runtime
    monkeypatch.setattr(module, 'members', lambda groups: [])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='another application'))
    with pytest.raises(ValueError, match='another process'):
        module.stop(path)
    assert signals == []


def test_already_stopped_is_idempotent(runtime, monkeypatch):
    path, signals = runtime
    monkeypatch.setattr(module, 'members', lambda groups: [])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    assert module.stop(path)['remaining_owned_pids'] == []
    assert signals == []
