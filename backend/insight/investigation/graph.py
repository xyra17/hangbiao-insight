from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone
from langgraph.graph import END, START, StateGraph
from psycopg.conninfo import make_conninfo

from insight.access import products_for, require_role
from insight.metrics.facts import authorize_object, source_manifest
from insight.models import InvestigationRun, InvestigationStep, MetricSnapshot, SemanticClaim, SemanticRun
from insight.services import audit

from .checkpoints import FencedPostgresSaver


class InvestigationState(TypedDict, total=False):
    query: str
    asin: str
    product_id: str | None
    tools: list[str]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    clarification: str
    result: dict[str, Any]
    stop_reason: str
    steps: int
    source_manifest: dict
    search_start: str
    search_end: str
    input_versions: dict
    write_requested: bool
    write_result: dict[str, Any]


def assert_execution_owner(run, locked):
    if locked.lease_token != run.lease_token:
        raise RuntimeError("investigation_lease_superseded")
    if hasattr(run, "_lock_pid"):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=%s AND classid=%s AND objid=%s AND objsubid=1 AND granted)",
                [run._lock_pid, run._lock_key >> 32, run._lock_key & 0xFFFFFFFF],
            )
            if not cursor.fetchone()[0]:
                raise RuntimeError("investigation_lock_lost")


def _persist(run: InvestigationRun, node: str, state: InvestigationState, output: dict[str, Any], tool=""):
    with transaction.atomic():
        locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
        assert_execution_owner(run, locked)
        require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
        if locked.status == "cancelled":
            raise RuntimeError("investigation_cancelled")
        if locked.steps_used >= locked.max_steps:
            raise RuntimeError("investigation_budget_exhausted")
        sequence = locked.steps_used + 1
        state["steps"] = sequence
        InvestigationStep.objects.create(
            run=locked,
            sequence=sequence,
            node=node,
            tool_name=tool,
            status="completed",
            input={"query": state.get("query", ""), "asin": state.get("asin", "")},
            output=output,
            evidence_refs=[item.get("id") for item in state.get("evidence", []) if item.get("id")],
            logical_key=tool or node,
            state_snapshot=dict(state),
        )
        locked.steps_used = sequence
        locked.checkpoint_version += 1
        locked.state = dict(state)
        locked.input_versions = state.get("input_versions", locked.input_versions)
        if state.get("asin"):
            authorize_object(run.requested_by, run.shop, state["asin"], run.product_id)
            locked.child_asin = state["asin"]
        locked.save(update_fields=["steps_used", "checkpoint_version", "state", "child_asin", "input_versions"])
        audit(run.requested_by, run.shop, "investigation.step_completed", run.pk,
              after={"sequence": sequence, "logical_key": tool or node}, request_id=str(run.pk))


def build_graph(run: InvestigationRun, checkpointer=None):
    def locate(state: InvestigationState):
        require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
        if "source_manifest" not in state:
            state["source_manifest"] = source_manifest(run.shop)
        if "search_start" not in state:
            end = timezone.now().astimezone(ZoneInfo(run.shop.timezone)).date()
            state["search_start"], state["search_end"] = str(end - timedelta(days=90)), str(end)
        query = state["query"]
        asin = state.get("asin") or next(iter(re.findall(r"\b[A-Z0-9]{10}\b", query.upper())), "")
        if not asin:
            candidates = list(
                products_for(run.requested_by)
                .filter(shop=run.shop)
                .values_list("child_asin", flat=True)
                .distinct()
            )
            if len(candidates) == 1:
                asin = candidates[0]
            elif not candidates:
                state["clarification"] = "没有找到当前店铺中可访问的商品，请提供子 ASIN。"
            else:
                state["clarification"] = "找到多个可访问商品，请补充子 ASIN 或 SKU。"
        if asin:
            authorize_object(run.requested_by, run.shop, asin, run.product_id)
            state["asin"] = asin
            frozen_metric = MetricSnapshot.objects.filter(
                shop=run.shop, child_asin=asin, product_id=run.product_id,
                source_manifest=state["source_manifest"],
                **({"pk": state["input_versions"]["metric_snapshot"]} if run.read_only_refresh else {}),
            ).exclude(payload__contains={"mode": "historical"}).order_by("-created_at").first()
            frozen_semantic = SemanticRun.objects.filter(
                shop=run.shop, child_asin=asin, product_id=run.product_id,
                source_manifest=state["source_manifest"], status__in=["completed", "partial"],
                **({"pk": state["input_versions"]["semantic_run"]} if run.read_only_refresh else {}),
            ).order_by("-created_at").first()
            state["input_versions"] = {
                "metric_snapshot": str(frozen_metric.pk) if frozen_metric else None,
                "semantic_run": str(frozen_semantic.pk) if frozen_semantic else None,
                "source_manifest": state["source_manifest"],
                "mapping_versions": frozen_metric.payload.get("mapping_versions", {}) if frozen_metric else {},
                "rule_version": frozen_metric.rule_version if frozen_metric else None,
                "workflow_version": "investigation-fixed-v2",
            }
        _persist(run, "locate", state, {"asin": asin, "clarification": state.get("clarification", "")})
        return state

    def choose(state: InvestigationState):
        text = state["query"].lower()
        tools = ["metric_snapshot"]
        if any(word in text for word in ("退货", "退款", "return", "refund", "评论", "review")):
            tools.append("review_claims")
        # Internal writes are explicit and bounded.  A normal investigation stays
        # read-only; only a request for a follow-up/check task enables the write node.
        explicit_write = any(
            word in text for word in ("创建任务", "建立任务", "核查任务", "跟进任务", "安排核查", "create task", "follow-up")
        )
        refusal = any(
            word in text for word in (
                "不要创建", "不要建立", "不要安排", "无需创建", "无需建立", "不要生成",
                "do not create", "without task",
            )
        )
        # A UI/API opt-in is the strongest signal. Natural-language intent is
        # accepted only when the query is affirmative; negated text can never
        # turn on a write capability by itself.
        state["write_requested"] = bool(state.get("write_requested")) or (explicit_write and not refusal)
        state["tools"] = tools
        _persist(run, "choose", state, {"tools": tools, "write_requested": state["write_requested"]})
        return state

    def metric(state: InvestigationState):
        authorize_object(run.requested_by, run.shop, state["asin"], run.product_id)
        snapshot = (
            MetricSnapshot.objects.filter(pk=state.get("input_versions", {}).get("metric_snapshot"), shop=run.shop, child_asin=state["asin"], product_id=run.product_id)
            .exclude(payload__contains={"mode": "historical"})
            .order_by("-created_at")
            .first()
        )
        if snapshot:
            signals = []
            for section in ("daily", "absolute", "cohort", "occurrence"):
                signals.extend(snapshot.payload.get(section, {}).get("signals", []))
            state.setdefault("evidence", []).append(
                {"id": str(snapshot.pk), "type": "metric_snapshot", "signals": signals}
            )
            state.setdefault("hypotheses", []).extend(
                {
                    "kind": "signal",
                    "metric": signal.get("metric"),
                    "title": signal.get("title"),
                    "confidence": "rule",
                }
                for signal in signals
            )
        else:
            state.setdefault("evidence", []).append(
                {"id": "metric:none", "type": "metric_snapshot", "status": "missing"}
            )
        _persist(run, "execute", state, {"found": bool(snapshot)}, "metric_snapshot")
        return state

    def reviews(state: InvestigationState):
        authorize_object(run.requested_by, run.shop, state["asin"], run.product_id)
        semantic = (
            SemanticRun.objects.filter(
                pk=state.get("input_versions", {}).get("semantic_run"),
                shop=run.shop,
                child_asin=state["asin"],
                product_id=run.product_id,
                status__in=["completed", "partial"],
            )
            .order_by("-created_at")
            .first()
        )
        claims = (
            list(SemanticClaim.objects.filter(run=semantic, prediction_status="accepted")[:50])
            if semantic
            else []
        )
        if claims:
            state.setdefault("evidence", []).append(
                {"id": str(semantic.pk), "type": "semantic_run", "claim_count": len(claims)}
            )
            state.setdefault("hypotheses", []).extend(
                {
                    "kind": "review",
                    "topic": claim.secondary_topic,
                    "polarity": claim.polarity,
                    "evidence_span": claim.evidence_span,
                    "claim_id": str(claim.pk),
                }
                for claim in claims
            )
        else:
            state.setdefault("evidence", []).append(
                {"id": "review:none", "type": "semantic_run", "status": "missing"}
            )
        _persist(run, "execute", state, {"claim_count": len(claims)}, "review_claims")
        return state

    def search_text(state: InvestigationState):
        from insight.semantic.retrieval import retrieve

        results = []
        for kind in ("reviews", "support"):
            result = retrieve(
                run.requested_by,
                run.shop,
                state["asin"],
                query=state["query"],
                start=date.fromisoformat(state["search_start"]),
                end=date.fromisoformat(state["search_end"]),
                product_id=run.product_id,
                kind=kind,
                limit=10,
                manifest=state["source_manifest"],
            )
            results.append(result)
            state.setdefault("evidence", []).append(
                {
                    "id": result["retrieval_id"],
                    "type": "text_retrieval",
                    "scope": result["scope"],
                    "source_manifest": result["source_manifest"],
                    "items": result["items"],
                    "limitations": result["limitations"],
                }
            )
        _persist(run, "execute", state, {"retrievals": results}, "text_evidence")
        return state

    def compose(state: InvestigationState):
        if state.get("write_requested") and "write_result" not in state:
            state["write_result"] = {
                "created": False,
                "status": "skipped",
                "reason": "调查剩余步骤不足以安全完成内部任务写入和结果保存。",
            }
        state["result"] = {
            "asin": state.get("asin", ""),
            "hypotheses": state.get("hypotheses", []),
            "evidence": state.get("evidence", []),
            "write": state.get("write_result"),
            "limitations": [
                "固定检查读取授权快照、语义 claim 和原文检索；当前不是模型自主决策调查。",
                "原文检索默认最近 90 天，检索样本不能证明因果或代表总体频率。",
            ],
        }
        state["stop_reason"] = "已完成允许的证据核查" if state.get("evidence") else "没有可用证据"
        _persist(run, "compose", state, {"stop_reason": state["stop_reason"]})
        return state

    def write_task(state: InvestigationState):
        """Create one unassigned internal follow-up through the domain command.

        The graph never receives a direct model/database write tool.  The command
        validates scope, frozen evidence, run ownership and idempotency.  A replay
        after a worker crash therefore returns the existing command receipt instead
        of creating another task.
        """
        from .commands import create_draft_task

        evidence_refs = [
            str(item["id"])
            for item in state.get("evidence", [])
            if item.get("id") and item.get("status") != "missing"
        ]
        if not evidence_refs:
            state["write_result"] = {
                "created": False,
                "status": "skipped",
                "reason": "没有可引用的已保存证据，未创建内部任务。",
            }
            _persist(run, "write_task", state, state["write_result"], "create_draft_task")
            return state
        asin = state.get("asin") or run.child_asin
        result = create_draft_task(
            run,
            {
                "logical_action_id": f"follow-up:{run.pk}",
                "title": f"核查 {asin} 调查发现",
                "question": "核对调查发现的异常方向，并记录人工核验结论。",
                "completion_criteria": "保留核验结果、处理依据和未解决限制。",
                "evidence_refs": evidence_refs,
            },
        )
        state["write_result"] = {"status": "created" if result.get("created") else "reused", **result}
        _persist(run, "write_task", state, state["write_result"], "create_draft_task")
        return state

    def replayable(key, function):
        def invoke(state):
            require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
            current = InvestigationRun.objects.get(pk=run.pk)
            assert_execution_owner(run, current)
            if current.status == "cancelled":
                raise RuntimeError("investigation_cancelled")
            if current.child_asin:
                authorize_object(run.requested_by, run.shop, current.child_asin, current.product_id)
            logical_keys = [key]
            if key == "write_task":
                # The persisted command receipt uses the concrete tool name;
                # the graph node is named after the user-facing capability.
                logical_keys.append("create_draft_task")
            saved = InvestigationStep.objects.filter(run=run, logical_key__in=logical_keys).order_by("sequence").first()
            if saved:
                return saved.state_snapshot
            # Each domain command and checkpoint owns its own transaction.  Do
            # not wrap the whole node in one outer transaction: an internal
            # task may commit before a worker crashes while recording the
            # checkpoint, and the command receipt must make the replay reuse
            # that task instead of rolling it back or duplicating it.
            with transaction.atomic():
                locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
                assert_execution_owner(run, locked)
            return function(state)
        return invoke

    graph = StateGraph(InvestigationState)
    for key, function in (("locate", locate), ("choose", choose), ("metric_snapshot", metric),
                          ("review_claims", reviews), ("text_evidence", search_text),
                          ("write_task", write_task), ("compose", compose)):
        graph.add_node(key, replayable(key, function))
    graph.add_edge(START, "locate")
    graph.add_conditional_edges(
        "locate",
        lambda state: "clarify" if state.get("clarification") else "choose",
        {"clarify": END, "choose": "choose"},
    )
    graph.add_edge("choose", "metric_snapshot")
    graph.add_conditional_edges(
        "metric_snapshot",
        lambda state: "review_claims" if "review_claims" in state.get("tools", []) else (
            "write_task"
            if state.get("write_requested") and int(state.get("steps", run.steps_used)) + 2 <= run.max_steps
            else "compose"
        ),
        {"review_claims": "review_claims", "write_task": "write_task", "compose": "compose"},
    )
    graph.add_edge("review_claims", "text_evidence")
    graph.add_conditional_edges(
        "text_evidence",
        lambda state: "write_task"
        if state.get("write_requested") and int(state.get("steps", run.steps_used)) + 2 <= run.max_steps
        else "compose",
        {"write_task": "write_task", "compose": "compose"},
    )
    graph.add_edge("write_task", "compose")
    graph.add_edge("compose", END)
    return graph.compile(checkpointer=checkpointer)


def execute_investigation(run: InvestigationRun):
    state: InvestigationState = {
        "query": run.query,
        "asin": run.child_asin,
        "product_id": str(run.product_id) if run.product_id else None,
        "evidence": [],
        "hypotheses": [],
        "steps": run.steps_used,
        "write_requested": bool(run.state.get("write_requested")),
    }
    if run.read_only_refresh and "source_manifest" in run.state:
        state["source_manifest"] = run.state["source_manifest"]
        state["input_versions"] = run.state["input_versions"]
    config = {"configurable": {"thread_id": str(run.pk)}}
    db = connection.settings_dict
    conninfo = make_conninfo(
        dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"], port=db["PORT"]
    )
    with FencedPostgresSaver.from_conn_string(conninfo) as saver:
        saver.conn.execute("SELECT pg_advisory_lock(78201344)")
        try:
            saver.setup()
        finally:
            saver.conn.execute("SELECT pg_advisory_unlock(78201344)")
        key = run.pk.int % (2**63 - 1)
        lock = saver.conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired, pg_backend_pid() AS pid", (key,)).fetchone()
        if not lock["acquired"]:
            raise RuntimeError("investigation_already_running")
        run._lock_pid, run._lock_key = lock["pid"], key
        with transaction.atomic():
            locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
            if locked.status in {"completed", "cancelled", "needs_clarification"}:
                return locked
            if locked.steps.filter(logical_key="").exists():
                locked.status, locked.stop_reason = "partial", "旧调查没有可验证的输入冻结记录，请发起新调查。"
                locked.save(update_fields=["status", "stop_reason"])
                return locked
            locked.lease_token += 1
            locked.status = "running"
            locked.save(update_fields=["lease_token", "status"])
            run.lease_token = locked.lease_token
        saver.run_id, saver.lease_token = run.pk, run.lease_token
        try:
            graph = build_graph(run, checkpointer=saver)
            existing = graph.get_state(config)
            result = graph.invoke(None if existing.values else state, config=config)
        except RuntimeError as exc:
            with transaction.atomic():
                locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
                assert_execution_owner(run, locked)
                if locked.status != "cancelled":
                    locked.status, locked.stop_reason = "partial", str(exc)
                    locked.save(update_fields=["status", "stop_reason"])
                return locked
        with transaction.atomic():
            locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
            assert_execution_owner(run, locked)
            if locked.status == "cancelled":
                return locked
            require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
            if result.get("asin"):
                authorize_object(run.requested_by, run.shop, result["asin"], run.product_id)
            if result.get("clarification"):
                locked.status, locked.stop_reason, locked.result = (
                    "needs_clarification", result["clarification"], {"clarification": result["clarification"]},
                )
            else:
                locked.status, locked.stop_reason, locked.result = (
                    "completed", result.get("stop_reason", ""), result.get("result", {}),
                )
            locked.state = result
            locked.save(update_fields=["status", "stop_reason", "result", "state"])
            audit(run.requested_by, run.shop, "investigation.finished", run.pk,
                  after={"status": locked.status, "input_versions": locked.input_versions}, request_id=str(run.pk))
            return locked
