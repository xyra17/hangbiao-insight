"""Opt-in real Celery/Redis failure drill. Uses only a disposable local Redis container."""
import os
import socket
import subprocess
import sys
import time
from datetime import timedelta
from uuid import uuid4

import pytest
import redis
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from insight.models import AuditEntry, Job, Membership, Outbox, Shop, Workspace
from insight.services import create_probe

pytestmark = pytest.mark.django_db(transaction=True)


def wait_for(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except redis.ConnectionError:
            pass
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for isolated worker state")


def test_real_broker_message_loss_outage_and_duplicate_recovery():
    name = "insight-s1-drill-" + uuid4().hex[:12]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    container = subprocess.check_output([
        "docker", "run", "--detach", "--name", name, "--publish", f"127.0.0.1:{port}:6379",
        "redis:7.4", "redis-server", "--save", "", "--appendonly", "no",
    ], text=True).strip()
    worker = None
    try:
        broker = redis.Redis(host="127.0.0.1", port=port, socket_connect_timeout=1, socket_timeout=1)
        wait_for(lambda: broker.ping())
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "PGDATABASE": connection.settings_dict["NAME"], "CELERY_BROKER_URL": f"redis://127.0.0.1:{port}/0"}
        actor = get_user_model().objects.create_user(username="broker-drill", password=None)
        workspace = Workspace.objects.create(name="隔离故障演练", is_test=True)
        Membership.objects.create(workspace=workspace, user=actor, is_admin=True)
        shop = Shop.objects.create(workspace=workspace, name="演练店铺", marketplace="US", timezone="UTC", currency="USD")
        job = create_probe(actor, shop, "lost-message", "broker-drill")

        def dispatch():
            return subprocess.run([sys.executable, "-c", "import django; django.setup(); from insight.tasks import dispatch_outbox; dispatch_outbox()"], env=env, capture_output=True, timeout=15)

        assert dispatch().returncode == 0
        assert broker.llen("celery") == 1
        assert Outbox.objects.get(job=job).sent_at is not None
        # Remove the actual queued message by restarting this disposable nonpersistent broker.
        subprocess.run(["docker", "restart", "--time", "1", container], check=True, capture_output=True)
        wait_for(lambda: broker.ping())
        assert broker.llen("celery") == 0
        worker = subprocess.Popen([
            sys.executable, "-m", "celery", "-A", "config", "worker", "--pool=solo", "--concurrency=1",
            "--without-gossip", "--without-mingle", "--without-heartbeat", "--loglevel=ERROR",
            "--hostname=" + name,
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Advance only the resend eligibility timestamp; the worker and broker are real.
        Outbox.objects.filter(job=job).update(sent_at=timezone.now() - timedelta(seconds=61))
        assert dispatch().returncode == 0
        wait_for(lambda: Job.objects.get(pk=job.pk).status == "completed")
        assert AuditEntry.objects.filter(action="job.completed", target_id=str(job.pk)).count() == 1
        duplicate = subprocess.run([sys.executable, "-c", "import django,sys; django.setup(); from insight.tasks import run_job; run_job.delay(sys.argv[1])", str(job.pk)], env=env, capture_output=True, timeout=15)
        assert duplicate.returncode == 0
        # A following FIFO job establishes that the duplicate delivery has been consumed.
        following = create_probe(actor, shop, "after-duplicate", "broker-drill")
        assert dispatch().returncode == 0
        wait_for(lambda: Job.objects.get(pk=following.pk).status == "completed")
        assert AuditEntry.objects.filter(action="job.completed", target_id=str(job.pk)).count() == 1
        worker.terminate()
        worker.wait(timeout=10)
        worker = None
        subprocess.run(["docker", "stop", "--time", "1", container], check=True, capture_output=True)
        pending = create_probe(actor, shop, "broker-down", "broker-drill")
        assert dispatch().returncode != 0
        assert Outbox.objects.get(job=pending).sent_at is None
        subprocess.run(["docker", "start", container], check=True, capture_output=True)
        wait_for(lambda: broker.ping())
        assert dispatch().returncode == 0
        worker = subprocess.Popen([
            sys.executable, "-m", "celery", "-A", "config", "worker", "--pool=solo", "--concurrency=1",
            "--without-gossip", "--without-mingle", "--without-heartbeat", "--loglevel=ERROR", "--hostname=" + name,
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: Job.objects.get(pk=pending.pk).status == "completed")
        assert AuditEntry.objects.filter(action="job.completed", target_id=str(pending.pk)).count() == 1
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        subprocess.run(["docker", "rm", "--force", container], check=True, capture_output=True)
