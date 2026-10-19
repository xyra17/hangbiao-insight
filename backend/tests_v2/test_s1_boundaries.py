"""S1 security boundaries discovered from the live URL registry."""
import uuid

import pytest
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.urls import URLPattern, URLResolver
from django.utils import timezone
from rest_framework.test import APIClient

from config.urls import urlpatterns

pytestmark = pytest.mark.django_db


def business_routes(patterns, prefix=""):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from business_routes(pattern.url_patterns, route)
        elif isinstance(pattern, URLPattern):
            if "format" in route or route.endswith("^$"):
                continue
            import re
            route = route.replace("^", "").replace("$", "")
            route = re.sub(r"\(\?P<\w+>[^)]+\)", str(uuid.UUID(int=1)), route)
            route = re.sub(r"<uuid:\w+>", str(uuid.UUID(int=1)), route)
            if route in {"api/v1/login/", "api/v1/session/", "api/v1/health/"}:
                continue
            callback = pattern.callback
            methods = getattr(callback, "actions", None)
            if methods is None:
                cls = getattr(callback, "cls", None)
                methods = [m for m in ("get", "post", "put", "patch", "delete") if cls and hasattr(cls, m)]
            for method in methods:
                yield method, "/" + route


ROUTES = list(business_routes(urlpatterns))


@pytest.mark.parametrize("method,url", ROUTES)
def test_all_registered_business_routes_deny_anonymous(method, url):
    client = APIClient()
    response = getattr(client, method)(url)
    assert response.status_code in {401, 403}, (method, url, response.status_code)
    assert response.data["request_id"] == response["X-Request-ID"]
    assert {"code", "message", "field_errors", "retryable", "request_id"} <= response.data.keys()


def test_expired_disabled_and_logged_out_sessions_cannot_read(setup_scope):
    users, _ = setup_scope
    for mode in ("expired", "disabled", "logout"):
        users["a"].is_active = True
        users["a"].save(update_fields=["is_active"])
        client = APIClient()
        assert client.login(username="a", password="unit-test-long-password")
        if mode == "expired":
            Session.objects.filter(session_key=client.session.session_key).update(expire_date=timezone.now())
        elif mode == "disabled":
            users["a"].is_active = False
            users["a"].save(update_fields=["is_active"])
        else:
            assert client.post("/api/v1/logout/").status_code == 204
        assert client.get("/api/v1/products/").status_code == 403
        assert not client.get("/api/v1/session/").data["authenticated"]


def test_login_throttle_rejects_excess_attempts_with_retry_header(monkeypatch):
    from types import SimpleNamespace

    from django.utils import timezone

    # All eleven requests must exercise one fixed minute bucket. A real
    # minute rollover tests a different contract and made this assertion flaky.
    fixed = timezone.now().replace(second=10, microsecond=0)
    monkeypatch.setattr("insight.throttling.timezone", SimpleNamespace(now=lambda: fixed))
    cache.clear()
    client = APIClient(enforce_csrf_checks=True)
    token = client.get("/api/v1/session/").data["csrf_token"]
    for _ in range(10):
        assert client.post("/api/v1/login/", {"username": "missing", "password": "wrong"}, HTTP_X_CSRFTOKEN=token).status_code == 403
    response = client.post("/api/v1/login/", {"username": "missing", "password": "wrong"}, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 429
    assert int(response["Retry-After"]) > 0
    assert not response.data["retryable"]
    cache.clear()


def test_csrf_and_unexpected_failure_use_redacted_error_envelope(client, setup_scope, monkeypatch, caplog):
    from insight import views
    users, _ = setup_scope
    csrf_client = APIClient(enforce_csrf_checks=True)
    response = csrf_client.post("/api/v1/login/", {"username": "a", "password": "secret-in-request"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert response.json()["request_id"] == response["X-Request-ID"]
    assert "secret-in-request" not in response.content.decode()

    def fail(*args, **kwargs):
        raise RuntimeError("provider-secret-must-not-be-echoed")

    monkeypatch.setattr(views, "shops_for", fail)
    client.force_authenticate(users["a"])
    response = client.get("/api/v1/shops/")
    assert response.status_code == 500
    assert response.data["request_id"] == response["X-Request-ID"]
    assert response.data["retryable"]
    assert "provider-secret-must-not-be-echoed" not in str(response.data) + caplog.text
