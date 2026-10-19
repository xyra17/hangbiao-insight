"""Per-tool authorization policy for investigation runs.

The planner only proposes a tool.  This module is the second, database-backed
decision point immediately before execution.  Keeping the matrix here makes
the allowed capabilities inspectable by the API and gives every denial an
auditable receipt without exposing data from an unauthorized scope.
"""

from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from insight.access import is_admin, require_role
from insight.metrics.facts import authorize_object
from insight.models import InvestigationRun, InvestigationToolCall, ShopGrant

_READ_ROLES = ("operator", "quality", "support", "data_admin")
TOOL_POLICIES = {
    "order_records": {"mode": "read", "roles": ("data_admin",), "description": "读取授权冻结订单、退款和退货样本"},
    "metric_snapshot": {"mode": "read", "roles": _READ_ROLES, "description": "读取冻结指标快照"},
    "review_claims": {"mode": "read", "roles": _READ_ROLES, "description": "读取已接受语义 claim"},
    "text_evidence": {"mode": "read", "roles": _READ_ROLES, "description": "读取授权范围原文证据"},
    "create_draft_task": {
        "mode": "internal_write",
        "roles": _READ_ROLES,
        "description": "创建未指派内部核查草稿",
    },
}


def permission_matrix() -> list[dict]:
    """Return a JSON-safe, immutable view of the tool capability matrix."""

    return [
        {
            "tool_name": name,
            "mode": policy["mode"],
            "roles": list(policy["roles"]),
            "description": policy["description"],
            "external_write": False,
        }
        for name, policy in TOOL_POLICIES.items()
    ]


def permitted_read_tools(run, names):
    """Only advertise current capabilities; execution still checks them again."""
    allowed = []
    for name in names:
        policy = TOOL_POLICIES.get(name)
        if not policy or policy["mode"] != "read":
            continue
        try:
            require_role(run.requested_by, run.shop, *policy["roles"])
            if name == "order_records":
                from insight.ingestion.service import require_source_access
                require_source_access(run.requested_by, run.shop)
        except PermissionDenied:
            continue
        allowed.append(name)
    return allowed


def _record_denial(run: InvestigationRun, tool_name: str, state: dict, *, key: str, reason: str):
    """Persist a denial without trusting model-provided scope details."""

    try:
        with transaction.atomic():
            locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
            existing = InvestigationToolCall.objects.filter(
                run=locked, idempotency_key=key
            ).first()
            if existing:
                return existing
            sequence = (
                InvestigationToolCall.objects.filter(run=locked)
                .order_by("-sequence")
                .values_list("sequence", flat=True)
                .first()
                or 0
            ) + 1
            return InvestigationToolCall.objects.create(
                run=locked,
                sequence=sequence,
                tool_name=tool_name[:80],
                scope={"shop": str(locked.shop_id), "child_asin": locked.child_asin},
                authorization="denied",
                input_snapshot={
                    "query": str(state.get("query", ""))[:2000],
                    "input_versions": state.get("input_versions", {}),
                },
                status="denied",
                error=reason[:300],
                idempotency_key=key,
            )
    except Exception:
        # The permission error remains the user-visible result.  A database
        # outage must never turn a denial into an apparently successful call.
        return None


def _authorize_tool(run: InvestigationRun, tool_name: str, state: dict, *, idempotency_key: str):
    denial_key = f"denied:{idempotency_key}"
    policy = TOOL_POLICIES.get(tool_name)
    if not policy:
        _record_denial(run, tool_name, state, key=denial_key, reason="unknown_tool")
        raise PermissionDenied("调查模型选择了未登记的工具。")
    asin = state.get("asin") or run.child_asin
    product_id = state.get("product_id") or run.product_id
    scope = {
        "shop": str(run.shop_id),
        "child_asin": asin or "",
        "product_id": str(product_id) if product_id else None,
        "source_manifest": state.get("source_manifest", {}),
    }
    with transaction.atomic():
        locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
        try:
            if locked.status == "cancelled":
                raise PermissionDenied("调查已取消。")
            if locked.lease_token != run.lease_token:
                raise PermissionDenied("调查执行代数已过期。")
            require_role(locked.requested_by, locked.shop, *policy["roles"])
            if tool_name == "order_records":
                from insight.ingestion.service import require_source_access
                require_source_access(locked.requested_by, locked.shop)
            if asin:
                authorize_object(locked.requested_by, locked.shop, asin, product_id)
            grant = ShopGrant.objects.filter(user=locked.requested_by, shop=locked.shop).first()
            admin = is_admin(locked.requested_by, locked.shop)
            if not grant and not admin:
                raise PermissionDenied("调查授权已撤销。")
            if policy["mode"] == "internal_write" and not state.get("write_requested"):
                raise PermissionDenied("当前调查没有明确授权内部草稿写入。")
        except PermissionDenied as exc:
            _record_denial(locked, tool_name, state, key=denial_key, reason=str(exc))
            raise
        call, created = InvestigationToolCall.objects.get_or_create(
            run=locked,
            idempotency_key=idempotency_key,
            defaults={
                "sequence": (InvestigationToolCall.objects.filter(run=locked).order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1,
                "tool_name": tool_name,
                "scope": scope,
                "authorization": "allowed",
                "grant_version": grant.version if grant else None,
                "input_snapshot": {"query": state.get("query", ""), "asin": asin or "", "input_versions": state.get("input_versions", {})},
                "status": "authorized",
            },
        )
        if not created and call.authorization != "allowed":
            raise PermissionDenied("工具调用已被撤权。")
        locked.tool_grants = {**(locked.tool_grants or {}), tool_name: grant.version if grant else 0}
        locked.save(update_fields=["tool_grants"])
    return call


def record_tool_result(call: InvestigationToolCall, *, refs: list[str] | None = None,
                       status: str = "completed", error: str = ""):
    InvestigationToolCall.objects.filter(pk=call.pk).update(
        output_refs=refs or [], status=status, error=error[:300]
    )


def authorize_tool(run: InvestigationRun, tool_name: str, state: dict, *, idempotency_key: str):
    try:
        return _authorize_tool(run, tool_name, state, idempotency_key=idempotency_key)
    except PermissionDenied as exc:
        # Persist after the authorization transaction rolls back.
        _record_denial(run, tool_name, state, key=f"denied:{idempotency_key}", reason=str(exc))
        raise
