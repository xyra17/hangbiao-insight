from datetime import date
from uuid import uuid4

import pytest
from django.db import connection
from psycopg.conninfo import make_conninfo

from insight.investigation import graph
from insight.investigation.checkpoints import FencedPostgresSaver
from insight.models import AuditEntry, InvestigationRun, MetricSnapshot

pytestmark = pytest.mark.django_db(transaction=True)


def make_run(metric_scope):
    users, shops = metric_scope
    return InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"],
        query="调查 B000TEST01 退货异常", child_asin="B000TEST01", max_steps=6)


def metric(run):
    return MetricSnapshot.objects.create(shop=run.shop, actor=run.requested_by,
        child_asin=run.child_asin, fingerprint=uuid4().hex, rule_version="frozen-rule",
        source_manifest={}, payload={}, local_date=date(2026, 9, 20))


def test_node_receipt_replays_after_checkpoint_failure_without_latest_reads(metric_scope, monkeypatch):
    run = make_run(metric_scope)
    first = metric(run)
    original = FencedPostgresSaver.put
    def broken(self, config, checkpoint, metadata, new_versions):
        if metadata.get("step", -1) == 1:
            raise RuntimeError("checkpoint write interrupted")
        return original(self, config, checkpoint, metadata, new_versions)
    monkeypatch.setattr(FencedPostgresSaver, "put", broken)
    stopped = graph.execute_investigation(run)
    assert stopped.status == "partial"
    assert stopped.steps.filter(logical_key="locate").count() == 1
    metric(run)
    monkeypatch.setattr(FencedPostgresSaver, "put", original)
    done = graph.execute_investigation(stopped)
    assert done.status == "completed" and done.steps_used == 6
    assert done.input_versions["metric_snapshot"] == str(first.pk)
    assert [x["id"] for x in done.result["evidence"] if x["type"] == "metric_snapshot"] == [str(first.pk)]
    assert done.steps.count() == 6
    assert AuditEntry.objects.filter(action="investigation.step_completed", target_id=str(run.pk)).count() == 6


def test_actual_lock_connection_death_blocks_domain_write(metric_scope, monkeypatch):
    run = make_run(metric_scope)
    original = graph._persist
    def disconnect(run, node, state, output, tool=""):
        if node == "choose":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s)", [run._lock_pid])
        return original(run, node, state, output, tool)
    monkeypatch.setattr(graph, "_persist", disconnect)
    with pytest.raises(Exception):
        graph.execute_investigation(run)
    run.refresh_from_db()
    assert run.steps.count() == 1
    assert run.steps_used == 1 and not run.result
    monkeypatch.setattr(graph, "_persist", original)
    done = graph.execute_investigation(run)
    assert done.status == "completed" and done.steps.count() == 6


def test_run_lock_blocks_second_job_without_mutating_state(metric_scope):
    from psycopg import connect
    run = make_run(metric_scope)
    db = connection.settings_dict
    info = make_conninfo(dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"], port=db["PORT"])
    with connect(info, autocommit=True) as owner:
        owner.execute("SELECT pg_advisory_lock(%s)", [run.pk.int % (2**63 - 1)])
        with pytest.raises(RuntimeError, match="already_running"):
            graph.execute_investigation(run)
        run.refresh_from_db()
        assert run.lease_token == 0 and run.steps_used == 0


def test_stale_generation_cannot_write_checkpoint(metric_scope):
    run = make_run(metric_scope)
    db = connection.settings_dict
    info = make_conninfo(dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"], port=db["PORT"])
    with FencedPostgresSaver.from_conn_string(info) as saver:
        saver.run_id, saver.lease_token = run.pk, run.lease_token
        InvestigationRun.objects.filter(pk=run.pk).update(lease_token=1)
        with pytest.raises(RuntimeError, match="lease_superseded"), saver._cursor():
            pytest.fail("stale writer reached the checkpoint cursor")


def test_real_process_kill_after_node_commit_before_checkpoint_replays_once(metric_scope, tmp_path):
    import os
    import subprocess
    import sys
    import time

    run = make_run(metric_scope)
    first = metric(run)
    marker = tmp_path / "node-committed"
    env = {**os.environ, "DJANGO_SETTINGS_MODULE":"config.settings", "PGDATABASE":connection.settings_dict["NAME"]}
    code = '''
import sys, time, django
from pathlib import Path
django.setup()
from insight.investigation.checkpoints import FencedPostgresSaver
from insight.investigation.graph import execute_investigation
from insight.models import InvestigationRun
original = FencedPostgresSaver.put
def stop_before_checkpoint(self, config, checkpoint, metadata, new_versions):
    if metadata.get("step") == 1:
        Path(sys.argv[2]).write_text("committed")
        time.sleep(30)
    return original(self, config, checkpoint, metadata, new_versions)
FencedPostgresSaver.put = stop_before_checkpoint
execute_investigation(InvestigationRun.objects.get(pk=sys.argv[1]))
'''
    proc = subprocess.Popen([sys.executable, "-c", code, str(run.pk), str(marker)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "investigation process did not commit its first node"
        proc.kill()
        proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    run.refresh_from_db()
    assert run.steps.filter(logical_key="locate").count() == 1
    metric(run)
    done = graph.execute_investigation(run)
    assert done.status == "completed" and done.steps.count() == done.steps_used == 6
    assert done.input_versions["metric_snapshot"] == str(first.pk)
