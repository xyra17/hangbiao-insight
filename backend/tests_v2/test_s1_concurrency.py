"""Independent PostgreSQL connections and an actual terminated worker process."""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections
from django.utils import timezone
from rest_framework.test import APIClient

from insight.models import AuditEntry, Job, Membership, Outbox, Product, ProductMapping
from insight.services import create_probe
from insight.tasks import dispatch_outbox, run_job

pytestmark = pytest.mark.django_db(transaction=True)


def parallel(call):
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return call()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        return [f.result(timeout=15) for f in futures]


def test_parallel_product_creation_and_mapping_revision_have_single_winner(setup_scope):
    users, shops = setup_scope
    data = {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "seller_sku": "s1", "fulfillment": "FBA", "name": "测试", "effective_from": "2026-01-01", "reason": "初始"}

    def post(path, body):
        client = APIClient()
        client.force_authenticate(users["admin"])
        return client.post(path, body, format="json").status_code

    assert sorted(parallel(lambda: post("/api/v1/products/", data))) == [201, 409]
    product = Product.objects.get()
    body = {"name": "更新", "effective_from": "2026-02-01", "reason": "修改映射", "expected_version": 1}
    assert sorted(parallel(lambda: post(f"/api/v1/products/{product.pk}/mappings/", body))) == [200, 409]
    assert ProductMapping.objects.count() == 2
    assert AuditEntry.objects.filter(action="product.created").count() == 1
    assert AuditEntry.objects.filter(action="product.mapping_revised").count() == 1


def test_parallel_password_reset_has_single_winner(setup_scope):
    users, shops = setup_scope
    member = Membership.objects.get(user=users["a"])

    def reset():
        client = APIClient()
        client.force_authenticate(users["admin"])
        return client.post(f"/api/v1/workspaces/{shops[0].workspace_id}/reset-password/", {
            "member": str(member.pk), "expected_version": 1, "password": "new-long-test-password", "reason": "测试",
        }, format="json").status_code

    assert sorted(parallel(reset)) == [200, 409]
    assert AuditEntry.objects.filter(action="member.password_reset").count() == 1


def test_parallel_probe_submission_and_execution_are_idempotent(setup_scope):
    users, shops = setup_scope
    ids = parallel(lambda: str(create_probe(users["admin"], shops[0], "parallel", "test").pk))
    assert ids[0] == ids[1]
    assert Outbox.objects.count() == 1
    parallel(lambda: run_job(ids[0]))
    assert Job.objects.get().status == "completed"
    assert AuditEntry.objects.filter(action="job.completed").count() == 1


def test_worker_process_death_rolls_back_partial_write_and_retry_completes_once(setup_scope, tmp_path):
    users, shops = setup_scope
    job = create_probe(users["admin"], shops[0], "kill-recover", "test")
    marker = tmp_path / "inside-transaction"
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "PGDATABASE": connection.settings_dict["NAME"]}
    code = '''
import sys, time, django
from pathlib import Path
django.setup()
from insight import tasks
original = tasks.audit
def blocked(*args, **kwargs):
    result = original(*args, **kwargs)
    Path(sys.argv[2]).write_text("ready")
    time.sleep(30)
    return result
tasks.audit = blocked
tasks.run_job(sys.argv[1])
'''
    proc = subprocess.Popen([sys.executable, "-c", code, str(job.pk), str(marker)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "worker did not reach transaction"
        # A duplicate delivery cannot enter while the first process owns the DB lock.
        run_job(str(job.pk))
        job.refresh_from_db()
        assert job.attempts == 1
        proc.kill()
        proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    assert not AuditEntry.objects.filter(action="job.completed").exists()
    run_job(str(job.pk))
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "completed" and job.attempts == 2
    assert AuditEntry.objects.filter(action="job.completed").count() == 1


def test_broker_failure_keeps_outbox_recoverable_and_future_retry_is_not_early(setup_scope, monkeypatch):
    from datetime import timedelta
    users, shops = setup_scope
    job = create_probe(users["admin"], shops[0], "broker-failure", "test")

    def unavailable(*args, **kwargs):
        raise ConnectionError("simulated broker outage")

    monkeypatch.setattr(run_job, "apply_async", unavailable)
    with pytest.raises(ConnectionError):
        dispatch_outbox()
    assert Outbox.objects.get(job=job).sent_at is None
    sent = []
    monkeypatch.setattr(run_job, "apply_async", lambda args, **kwargs: sent.append(args[0]))
    dispatch_outbox()
    assert sent == [str(job.pk)]
    job.status = "retry_wait"
    job.next_run_at = timezone.now() + timedelta(minutes=1)
    job.save()
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.attempts == 0


def test_login_limit_is_shared_by_concurrent_connections_and_does_not_store_identity(setup_scope):
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from insight.models import LoginThrottleBucket
    from insight.throttling import LoginThrottle

    # Use GET to avoid manually installing DRF parsers on the Request wrapper.
    request = Request(APIRequestFactory().get("/api/v1/login/"))
    for _ in range(9):
        assert LoginThrottle().allow_request(request, None)
    assert sorted(parallel(lambda: LoginThrottle().allow_request(request, None))) == [False, True]
    assert all(len(key) == 64 for key in LoginThrottleBucket.objects.values_list("key", flat=True))


def test_stale_worker_cannot_overwrite_new_lease_receipt(setup_scope, monkeypatch):
    from insight import tasks
    users, shops = setup_scope
    job = create_probe(users["admin"], shops[0], "stale-lease", "test")
    original = tasks.require_role

    def supersede(*args, **kwargs):
        original(*args, **kwargs)
        Job.objects.filter(pk=job.pk).update(lease_token=2, status="running", result={"owner": "new-attempt"})

    monkeypatch.setattr(tasks, "require_role", supersede)
    with pytest.raises(RuntimeError, match="lease superseded"):
        run_job(str(job.pk))
    job.refresh_from_db()
    assert job.lease_token == 2 and job.result == {"owner": "new-attempt"}
    assert job.status == "running"
    assert not AuditEntry.objects.filter(action="job.completed").exists()


def test_parallel_admin_demotion_cannot_remove_last_admin(setup_scope):
    users, shops = setup_scope
    Membership.objects.filter(user=users["a"]).update(is_admin=True)
    admins = [users["admin"], users["a"]]
    barrier = Barrier(2)

    def demote(user):
        close_old_connections()
        try:
            member = Membership.objects.get(user=user)
            client = APIClient()
            client.force_authenticate(user)
            barrier.wait(timeout=10)
            return client.post(f"/api/v1/workspaces/{shops[0].workspace_id}/member-role/", {
                "member": str(member.pk), "expected_version": 1, "is_admin": False,
            }, format="json").status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, admins))
    assert sorted(outcomes) == [200, 400]
    assert Membership.objects.filter(is_admin=True).count() == 1
