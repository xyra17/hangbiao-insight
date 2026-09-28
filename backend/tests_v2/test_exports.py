import csv
import io

import pytest

from insight.models import AuditEntry

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("file_type", ["json", "html"])
def test_report_export_rechecks_scope_and_preserves_frozen_versions(artifacts, client, file_type):
    users, shops, grant, groups = artifacts
    report = groups[0]["reports"]
    report.report_version = 3
    report.payload = {"summary": "<script>alert('x')</script>", "limitations": ["仅代表采集样本"], "gaps": ["缺少退款"], "citations": [{"id": str(report.metric_snapshot_id)}], "claims": [], "signals": []}
    report.save()
    client.force_authenticate(users["restricted"])
    url = f"/api/v1/reports/{report.pk}/export/?file_type={file_type}"
    response = client.get(url)
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store, private"
    assert "attachment" in response["Content-Disposition"]
    if file_type == "json":
        data = response.json()
        assert data["report_version"] == 3
        assert data["data_cutoff"] == "2026-09-01"
        assert data["metric_source_manifest"] == {}
        assert data["payload"] == report.payload
    else:
        text = response.content.decode()
        assert "<script>" not in text and "&lt;script&gt;" in text
        assert "仅代表采集样本" in text and "缺少退款" in text
        assert "default-src 'none'" in response["Content-Security-Policy"]
    assert AuditEntry.objects.filter(action="report.exported", target_id=str(report.pk)).count() == 1
    for group in groups[1:]:
        assert client.get(f"/api/v1/reports/{group['reports'].pk}/export/?file_type={file_type}").status_code == 404
    grant.products.clear()
    assert client.get(url).status_code == 404


def test_csv_export_filters_scope_and_status_and_neutralizes_formula(artifacts, client):
    users, shops, grant, groups = artifacts
    event = groups[0]["events"]
    event.title = ' \t=HYPERLINK("https://invalid.example","click")'
    event.save()
    client.force_authenticate(users["restricted"])
    response = client.get(f"/api/v1/events/export/?shop={shops[0].pk}&status=pending")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 2
    assert rows[1][0] == str(event.pk)
    assert rows[1][6].startswith("'")
    assert rows[1][10] == "1"
    assert AuditEntry.objects.filter(action="events.exported").count() == 1
    assert len(list(csv.reader(io.StringIO(client.get(f"/api/v1/events/export/?shop={shops[0].pk}&status=closed").content.decode("utf-8-sig"))))) == 1
    grant.products.clear()
    assert len(list(csv.reader(io.StringIO(client.get("/api/v1/events/export/").content.decode("utf-8-sig"))))) == 1


def test_export_audit_failure_never_returns_artifact(artifacts, client, monkeypatch):
    from insight import exports
    users, _, _, groups = artifacts
    client.force_authenticate(users["restricted"])

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(exports, "audit", fail)
    response = client.get(f"/api/v1/reports/{groups[0]['reports'].pk}/export/?file_type=json")
    assert response.status_code == 500
    assert "payload" not in response.json()
    assert not AuditEntry.objects.exists()


@pytest.mark.parametrize("value", ["=1+1", "+cmd", "-cmd", "@cmd", "\tplain", "\rplain", "\nplain", "\ufeff=1+1", "  =1+1"])
def test_csv_formula_control_prefixes(value):
    from insight.exports import safe_csv
    assert safe_csv(value).startswith("'")
    assert safe_csv("正常描述") == "正常描述"
