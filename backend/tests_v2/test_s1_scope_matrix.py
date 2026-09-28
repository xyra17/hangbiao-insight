"""Cross-shop and partial-SKU visibility for every existing derived artifact family."""

import pytest

from insight.models import (
    Job,
    StandardReport,
)

pytestmark = pytest.mark.django_db




@pytest.mark.parametrize("endpoint", ["snapshots", "events", "observations", "reports", "semantic-runs", "action-tasks", "change-proposals", "investigations", "notifications", "jobs"])
def test_list_detail_filter_and_revocation_for_derived_artifacts(artifacts, client, endpoint):
    users, shops, grant, groups = artifacts
    client.force_authenticate(users["restricted"])
    response = client.get(f"/api/v1/{endpoint}/")
    assert response.status_code == 200, response.data
    assert {row["id"] for row in response.data["results"]} == {str(groups[0][endpoint].pk)}
    assert client.get(f"/api/v1/{endpoint}/{groups[0][endpoint].pk}/").status_code == 200
    for group in groups[1:]:
        assert client.get(f"/api/v1/{endpoint}/{group[endpoint].pk}/").status_code == 404
    foreign = client.get(f"/api/v1/{endpoint}/?shop={shops[1].pk}")
    assert foreign.status_code == 404 or foreign.data["count"] == 0
    grant.products.clear()
    assert client.get(f"/api/v1/{endpoint}/").data["count"] == 0
    assert client.get(f"/api/v1/{endpoint}/{groups[0][endpoint].pk}/").status_code == 404


def test_investigation_receipt_disappears_with_revoked_product(artifacts, client):
    users, shops, grant, groups = artifacts
    run = groups[0]["investigations"]
    job = Job.objects.create(shop=shops[0], actor=users["restricted"], kind="investigation_run", idempotency_key="run-receipt", payload={"run_id": str(run.pk)}, result={"investigation_id": str(run.pk), "status": "completed"})
    client.force_authenticate(users["restricted"])
    assert client.get(f"/api/v1/jobs/{job.pk}/").status_code == 200
    grant.products.clear()
    assert client.get(f"/api/v1/jobs/{job.pk}/").status_code == 404


def test_unknown_and_mismatched_aggregate_cannot_bypass_scope(artifacts, client):
    users, shops, _, _ = artifacts
    report = StandardReport.objects.create(shop=shops[0], child_asin="B000ABSENT", publication_key="unknown-object", payload={"secret": "hidden"})
    client.force_authenticate(users["restricted"])
    assert client.get(f"/api/v1/reports/{report.pk}/").status_code == 404
