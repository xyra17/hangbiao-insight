import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from celery import Celery
from django.conf import settings
from psycopg import sql
from redis import Redis
from redis.exceptions import ConnectionError


@pytest.mark.django_db(transaction=True)
def test_two_celery_environments_do_not_consume_each_others_tasks(tmp_path, monkeypatch):
    """Exercise project routing, Redis prefix, database ownership and retry."""
    root = Path(__file__).resolve().parents[2]
    runtime = root / ".local" / "isolation-drills" / uuid.uuid4().hex
    runtime.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    redis_process = subprocess.Popen(
        [shutil.which("redis-server"), "--bind", "127.0.0.1", "--port", str(port),
         "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    monkeypatch.setenv("CELERY_BROKER_URL", f"redis://127.0.0.1:{port}/0")
    cfg = settings.DATABASES["default"]
    connection_args = dict(host=cfg["HOST"], port=cfg["PORT"], user=cfg["USER"], password=cfg["PASSWORD"])
    admin = psycopg.connect(dbname="postgres", autocommit=True, **connection_args)
    names = ["isolation_normal_" + uuid.uuid4().hex[:12], "isolation_accept_" + uuid.uuid4().hex[:12]]
    processes, databases, logs = [], [], []

    def wait_until(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError("isolated worker did not reach the expected state")

    def rows(db):
        return db.execute("SELECT * FROM isolation_probe").fetchall()

    def worker(name):
        env = {**os.environ, "PGDATABASE": name, "CELERY_BROKER_URL": f"redis://127.0.0.1:{port}/0",
               "INSIGHT_CELERY_NAMESPACE": name, "INSIGHT_ACCEPTANCE_ENV": ""}
        log = (runtime / f"{name}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, str(root / "scripts/acceptance_process.py"), sys.executable,
             "-m", "celery", "-A", "config.celery", "worker", "-I", "tests_v2.isolation_tasks",
             "--pool=solo", "--concurrency=1", "--without-gossip", "--without-mingle", "--loglevel=INFO"],
            cwd=root / "backend", env=env, stdout=log, stderr=log,
        )
        processes.append(process)
        wait_until(lambda: "ready." in (runtime / f"{name}.log").read_text())

    def producer(name):
        app = Celery("probe", broker=f"redis://127.0.0.1:{port}/0")
        app.conf.update(task_default_queue=name, task_default_exchange=name, task_default_routing_key=name,
                        task_routes={"*": {"queue": name}},
                        broker_transport_options={"global_keyprefix": name + ":"})
        return app

    try:
        wait_until(lambda: redis_process.poll() is None and _redis_ready(port))
        for name in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            db = psycopg.connect(dbname=name, autocommit=True, **connection_args)
            databases.append(db)
            db.execute("CREATE TABLE isolation_probe(marker text, database_name text, queue text, retries int)")
        worker(names[0])
        producer(names[1]).send_task("isolation_probe.record", args=["acceptance", True])
        time.sleep(1)
        assert not rows(databases[0]) and not rows(databases[1])
        producer(names[0]).send_task("isolation_probe.record", args=["normal"])
        wait_until(lambda: bool(rows(databases[0])))
        worker(names[1])
        wait_until(lambda: bool(rows(databases[1])))
        assert rows(databases[0]) == [("normal", names[0], names[0], 0)]
        assert rows(databases[1]) == [("acceptance", names[1], names[1], 1)]
        (runtime / "result.json").write_text(json.dumps({"status": "passed", "database_queue_ownership": True,
                                                        "retry_ownership": True, "shared_redis_touched": False}))
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=15)
        for log in logs:
            log.close()
        for db in databases:
            db.close()
        for name in names:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
        redis_process.terminate()
        redis_process.wait(timeout=5)
        (runtime / "cleanup.json").write_text(json.dumps({"owned_processes_stopped": all(p.poll() is not None for p in processes),
                                                         "temporary_redis_stopped": redis_process.poll() is not None,
                                                         "temporary_databases_removed": True}))


def _redis_ready(port):
    try:
        return Redis(host="127.0.0.1", port=port).ping()
    except ConnectionError:
        return False
