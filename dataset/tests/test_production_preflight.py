import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("production_preflight", Path(__file__).resolve().parents[2] / "scripts/production_preflight.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("url", ["", "redis://", "http://redis/0", "redis://redis/0", "redis://:pw@0.0.0.0:6379/0", "redis://:pw@redis:99999/0", "redis://:pw@redis/not-a-db", "redis://:pw@127.0.0.1:6379/0"])
def test_missing_malformed_unauthenticated_or_wrong_network_broker_fails(url):
    assert not MODULE.broker_check(url, "container")


def test_declared_network_supports_private_dns_and_host_loopback():
    assert MODULE.broker_check("redis://:password@redis:6379/0", "container")
    assert MODULE.broker_check("rediss://:password@internal-redis:6379/2", "container")
    assert MODULE.broker_check("redis://:password@127.0.0.1:6379/0", "host")


def test_preflight_requires_actual_injected_environment_and_hides_secrets(tmp_path):
    assert MODULE.inspect_configuration({})["status"] == "failed"
    env = {"DJANGO_DEBUG": "0", "DJANGO_SECRET_KEY": "s" * 40, "INSIGHT_DB_PASSWORD": "secret-password",
           "CELERY_BROKER_URL": "redis://:private@redis:6379/0", "DJANGO_ALLOWED_HOSTS": "app.example.invalid",
           "INSIGHT_TLS_TERMINATED": "1", "INSIGHT_STORAGE_ROOT": str(tmp_path)}
    result = MODULE.inspect_configuration(env, allow_root=True)
    assert result["status"] == "passed" and "secret-password" not in str(result) and "private@" not in str(result)
    assert MODULE.inspect_configuration({**env, "DJANGO_ALLOWED_HOSTS": "*"}, allow_root=True)["status"] == "failed"
