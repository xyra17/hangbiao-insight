import pytest

from insight.models import ActionExecution, ChangeProposal, Product
from insight.proposals import action_policy

pytestmark = pytest.mark.django_db


def test_action_policy_only_allows_internal_task_assignment():
    assert action_policy("internal_followup") == "internal_task_assignment"
    assert action_policy("price_change") == "manual_only"


def test_action_task_review_and_proposal_need_version_and_approval(metric_scope, client):
    users, shops = metric_scope
    product = Product.objects.get(shop=shops[0])
    client.force_authenticate(users["a"])
    created = client.post(
        "/api/v1/action-tasks/",
        {
            "shop": str(shops[0].pk),
            "child_asin": product.child_asin,
            "product": str(product.pk),
            "title": "核对页面可售状态",
        },
        format="json",
    )
    assert created.status_code == 201
    task = created.data
    completed = client.post(
        f"/api/v1/action-tasks/{task['id']}/complete/",
        {
            "notes": "已核对页面和库存报表",
            "evidence_refs": [],
            "expected_version": task["version"],
        },
        format="json",
    )
    assert completed.status_code == 200
    assert ActionExecution.objects.count() == 1
    stale = client.post(
        f"/api/v1/action-tasks/{task['id']}/review/",
        {
            "status": "scheduled",
            "review_at": "2026-09-30T08:00:00Z",
            "expected_version": task["version"],
        },
        format="json",
    )
    assert stale.status_code == 409
    proposal = client.post(
        "/api/v1/change-proposals/",
        {
            "shop": str(shops[0].pk),
            "child_asin": product.child_asin,
            "product": str(product.pk),
            "operation": "price_change",
            "target": {"field": "price"},
            "before": {"price": "20"},
            "after": {"price": "18"},
            "evidence_refs": ["snapshot:demo"],
            "idempotency_key": "proposal-1",
        },
        format="json",
    )
    assert proposal.status_code == 201 and proposal.data["status"] == "pending"
    forbidden = client.post(
        f"/api/v1/change-proposals/{proposal.data['id']}/review/",
        {
            "decision": "approve",
            "note": "请审核",
            "expected_version": 1,
            "payload_hash": proposal.data["payload_hash"],
        },
        format="json",
    )
    assert forbidden.status_code == 403
    client.force_authenticate(users["admin"])
    approved = client.post(
        f"/api/v1/change-proposals/{proposal.data['id']}/review/",
        {
            "decision": "approve",
            "note": "证据充分，批准内部提案记录",
            "expected_version": 1,
            "payload_hash": proposal.data["payload_hash"],
        },
        format="json",
    )
    assert approved.status_code == 200 and approved.data["status"] == "approved"
    assert ChangeProposal.objects.get(pk=proposal.data["id"]).after["price"] == "18"


def test_task_and_proposal_aggregate_scope_is_hidden_and_patch_disabled(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    task = client.post(
        "/api/v1/action-tasks/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "title": "核查证据",
        },
        format="json",
    )
    assert task.status_code == 201
    assert (
        client.patch(
            f"/api/v1/action-tasks/{task.data['id']}/", {"status": "completed"}, format="json"
        ).status_code
        == 405
    )
    client.force_authenticate(users["restricted"])
    assert client.get("/api/v1/action-tasks/").data["count"] == 0
    assert (
        client.post(
            f"/api/v1/action-tasks/{task.data['id']}/complete/",
            {
                "notes": "越权操作",
                "expected_version": 1,
            },
            format="json",
        ).status_code
        == 404
    )


def test_task_rejects_duplicate_evidence_references(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/action-tasks/",
        {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "title": "核查", "evidence_refs": ["snapshot:1", "snapshot:1"]},
        format="json",
    )
    assert response.status_code == 400


def test_open_task_draft_can_be_revised_with_version_guard(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    created = client.post(
        "/api/v1/action-tasks/",
        {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "title": "初稿"},
        format="json",
    ).data
    url = f"/api/v1/action-tasks/{created['id']}/revise-draft/"
    revised = client.post(url, {"expected_version": 1, "title": "核对包装", "evidence_refs": ["review:r1"]}, format="json")
    assert revised.status_code == 200 and revised.data["version"] == 2
    assert client.post(url, {"expected_version": 1, "title": "过期修改"}, format="json").status_code == 409


def test_external_proposal_requires_explicit_human_handoff_and_never_executes(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    created = client.post(
        "/api/v1/change-proposals/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "operation": "price_change",
            "target": {"field": "price", "currency": "USD"},
            "before": {"value": "20.00"},
            "after": {"value": "18.00"},
            "evidence_refs": ["snapshot:current"],
            "idempotency_key": "manual-price-1",
        },
        format="json",
    )
    assert created.status_code == 201
    proposal = created.data
    client.force_authenticate(users["admin"])
    approved = client.post(
        f"/api/v1/change-proposals/{proposal['id']}/review/",
        {
            "decision": "approve",
            "note": "已核对当前价格证据",
            "expected_version": 1,
            "payload_hash": proposal["payload_hash"],
        },
        format="json",
    )
    assert approved.status_code == 200
    handoff = client.post(
        f"/api/v1/change-proposals/{proposal['id']}/authorize-manual/",
        {"expected_version": approved.data["version"], "note": "交给运营在 Seller Central 执行", "handoff_ref": "SC-PRICE-001"},
        format="json",
    )
    assert handoff.status_code == 200
    assert handoff.data["status"] == "manual_authorized"
    assert handoff.data["manual_authorization"]["handoff_ref"] == "SC-PRICE-001"
    assert handoff.data["execution_result"]["external_execution"]["status"] == "not_executed"
    blocked = client.post(
        f"/api/v1/change-proposals/{proposal['id']}/execute/",
        {"expected_version": handoff.data["version"], "payload_hash": proposal["payload_hash"]},
        format="json",
    )
    assert blocked.status_code == 400


def test_external_handoff_rechecks_approved_payload_before_recording_authorization(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    created = client.post(
        "/api/v1/change-proposals/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "operation": "inventory_change",
            "target": {"field": "available", "warehouse": "FBA"},
            "before": {"value": 10},
            "after": {"value": 20},
            "evidence_refs": ["snapshot:current"],
            "idempotency_key": "manual-inventory-hash-1",
        },
        format="json",
    )
    assert created.status_code == 201
    client.force_authenticate(users["admin"])
    approved = client.post(
        f"/api/v1/change-proposals/{created.data['id']}/review/",
        {
            "decision": "approve",
            "note": "确认库存调整仍需人工执行",
            "expected_version": 1,
            "payload_hash": created.data["payload_hash"],
        },
        format="json",
    )
    assert approved.status_code == 200
    ChangeProposal.objects.filter(pk=created.data["id"]).update(after={"value": 25})
    handoff = client.post(
        f"/api/v1/change-proposals/{created.data['id']}/authorize-manual/",
        {
            "expected_version": approved.data["version"],
            "note": "内容变化后不得沿用原批准",
            "handoff_ref": "SC-STOCK-HASH-001",
        },
        format="json",
    )
    assert handoff.status_code == 409
