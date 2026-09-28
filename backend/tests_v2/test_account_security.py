"""Real session cookies, CSRF and account-wide revocation."""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from insight.models import AuditEntry, Membership, SessionVersion, Workspace

pytestmark = pytest.mark.django_db


def login_as(username, password="unit-test-long-password"):
    client = APIClient(enforce_csrf_checks=True)
    token = client.get("/api/v1/session/").data["csrf_token"]
    response = client.post("/api/v1/login/", {"username": username, "password": password}, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 200, response.data
    return client


@pytest.fixture(autouse=True)
def clear_throttles():
    cache.clear()
    yield
    cache.clear()


def test_reset_invalidates_all_old_sessions_and_password_without_affecting_others(client, setup_scope):
    users, shops = setup_scope
    first, second, other = login_as("a"), login_as("a"), login_as("b")
    member = Membership.objects.get(user=users["a"])
    client.force_authenticate(users["admin"])
    url = f"/api/v1/workspaces/{shops[0].workspace_id}/reset-password/"
    password = "new-unique-test-password-456"
    data = {"member": str(member.pk), "expected_version": 1, "password": password, "reason": "忘记密码"}
    response = client.post(url, data, format="json")
    assert response.status_code == 200
    assert client.post(url, data, format="json").status_code == 409
    assert first.get("/api/v1/shops/").status_code == 403
    assert not second.get("/api/v1/session/").data["authenticated"]
    assert other.get("/api/v1/shops/").status_code == 200
    assert not first.login(username="a", password="unit-test-long-password")
    assert login_as("a", password).get("/api/v1/shops/").status_code == 200
    users["a"].refresh_from_db()
    assert users["a"].check_password(password)
    audit = AuditEntry.objects.get(action="member.password_reset")
    assert audit.request_id
    assert password not in str(response.data) + str(list(AuditEntry.objects.values()))
    assert users["a"].password not in str(list(AuditEntry.objects.values()))


def test_revoke_preserves_password_but_invalidates_sessions(client, setup_scope):
    users, shops = setup_scope
    first, second = login_as("a"), login_as("a")
    member = Membership.objects.get(user=users["a"])
    client.force_authenticate(users["admin"])
    response = client.post(f"/api/v1/workspaces/{shops[0].workspace_id}/revoke-sessions/", {
        "member": str(member.pk), "expected_version": 1, "reason": "设备遗失",
    }, format="json")
    assert response.status_code == 200
    assert not first.get("/api/v1/session/").data["authenticated"]
    assert second.get("/api/v1/products/").status_code == 403
    assert login_as("a").get("/api/v1/shops/").status_code == 200
    assert AuditEntry.objects.filter(action="member.sessions_revoked").count() == 1


@pytest.mark.parametrize("action", ["reset-password", "revoke-sessions"])
def test_credentials_reject_non_admin_foreign_member_stale_and_cross_workspace(client, setup_scope, action):
    users, shops = setup_scope
    member = Membership.objects.get(user=users["a"])
    url = f"/api/v1/workspaces/{shops[0].workspace_id}/{action}/"
    data = {"member": str(member.pk), "expected_version": 1, "reason": "测试", "password": "long-new-password-456"}
    client.force_authenticate(users["a"])
    assert client.post(url, data, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    assert client.post(url, {**data, "expected_version": 8}, format="json").status_code == 409
    foreign = Workspace.objects.create(name="其他工作区")
    foreign_member = Membership.objects.create(workspace=foreign, user=users["b"])
    assert client.post(url, {**data, "member": str(foreign_member.pk)}, format="json").status_code == 404
    Membership.objects.create(workspace=foreign, user=users["a"])
    assert client.post(url, data, format="json").status_code == 403
    assert not SessionVersion.objects.exists()
    assert not AuditEntry.objects.exists()
    users["a"].refresh_from_db()
    assert users["a"].check_password("unit-test-long-password")


def test_weak_password_csrf_and_audit_failure_leave_credentials_unchanged(client, setup_scope, monkeypatch):
    users, shops = setup_scope
    member = Membership.objects.get(user=users["a"])
    admin_session = login_as("admin")
    url = f"/api/v1/workspaces/{shops[0].workspace_id}/reset-password/"
    data = {"member": str(member.pk), "expected_version": 1, "reason": "测试", "password": "short"}
    assert admin_session.post(url, data, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    assert client.post(url, data, format="json").status_code == 400
    from insight import administration

    def fail(*args, **kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(administration, "workspace_audit", fail)
    response = client.post(url, {**data, "password": "long-new-password-456"}, format="json")
    assert response.status_code == 500
    assert response.data["code"] == "internal_error"
    assert "audit storage unavailable" not in str(response.data)
    users["a"].refresh_from_db()
    member.refresh_from_db()
    assert member.version == 1
    assert users["a"].check_password("unit-test-long-password")
    assert not SessionVersion.objects.exists()


def test_admin_self_revocation_logs_out_current_session(setup_scope):
    users, shops = setup_scope
    admin = login_as("admin")
    member = Membership.objects.get(user=users["admin"])
    token = admin.get("/api/v1/session/").data["csrf_token"]
    assert admin.post(f"/api/v1/workspaces/{shops[0].workspace_id}/revoke-sessions/", {
        "member": str(member.pk), "expected_version": 1, "reason": "全部设备退出",
    }, format="json", HTTP_X_CSRFTOKEN=token).status_code == 200
    assert not admin.get("/api/v1/session/").data["authenticated"]
