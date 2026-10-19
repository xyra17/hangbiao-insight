"""Independent development cases and SDK substitutes; zero provider dispatch."""
# ruff: noqa: F811

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.semantic.factory import build_agent_planner, build_relevance_gateway
from app.semantic.structured_response import StructuredResponseError
from insight.semantic.relevance import CONSTRAINTS, RelevanceAdapter
from tests_v2.test_model_cost_readiness import approved_limits  # noqa: F401
from tests_v2.test_semantic import review_report  # noqa: F401


def response(status="completed", text="{}", reason=None):
    raw = {
        "status": status,
        "output_text": text,
        "id": "sdk-receipt",
        "model": "deepseek-flash",
        "usage": {"input_tokens": 20, "output_tokens": 10},
        "incomplete_details": {"reason": reason},
    }
    return SimpleNamespace(
        **{k: v for k, v in raw.items() if k not in {"usage", "incomplete_details"}},
        usage=SimpleNamespace(
            input_tokens=20, output_tokens=10, output_tokens_details=SimpleNamespace(reasoning_tokens=0)
        ),
        incomplete_details={"reason": reason},
        model_dump=lambda **kwargs: raw,
    )


def sdk(monkeypatch, value):
    import openai

    calls = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "nonfunctional-sdk-substitute")
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(
            responses=SimpleNamespace(create=lambda **payload: calls.append(payload) or value)
        ),
    )
    return calls


@pytest.mark.parametrize("kind", ["planner", "relevance"])
@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_separate_configuration_reaches_actual_sdk_payload_and_receipt(
    approved_limits, monkeypatch, kind, effort
):
    calls = sdk(monkeypatch, response())
    monkeypatch.setenv(
        "INVESTIGATION_REASONING_EFFORT" if kind == "planner" else "EVIDENCE_RELEVANCE_REASONING_EFFORT",
        effort,
    )
    gateway = build_agent_planner().gateway if kind == "planner" else build_relevance_gateway()
    kwargs = {} if kind == "planner" else {"max_output_tokens": 100}
    assert gateway.plan(system="system", user="user", schema={}, **kwargs) == "{}"
    assert len(calls) == 1 and calls[0]["reasoning"] == {"effort": effort}
    assert calls[0]["max_output_tokens"] == 100
    assert gateway.last_model_receipt["reasoning_effort"] == effort
    assert gateway.last_model_receipt["request_sha256"]


@pytest.mark.parametrize("kind", ["planner", "relevance"])
def test_invalid_reasoning_is_rejected_before_sdk_dispatch(approved_limits, monkeypatch, kind):
    calls = sdk(monkeypatch, response())
    monkeypatch.setenv(
        "INVESTIGATION_REASONING_EFFORT" if kind == "planner" else "EVIDENCE_RELEVANCE_REASONING_EFFORT",
        "invented",
    )
    with pytest.raises(ValueError):
        build_agent_planner() if kind == "planner" else build_relevance_gateway()
    assert not calls


@pytest.mark.parametrize("kind", ["planner", "relevance"])
@pytest.mark.parametrize(
    "status,text,reason,code",
    [
        ("incomplete", "{}", "max_output_tokens", "output_truncated"),
        ("incomplete", "{}", "other", "response_incomplete"),
        ("failed", "{}", None, "response_not_completed"),
        ("completed", "   ", None, "empty_output_text"),
    ],
)
def test_received_failure_has_raw_usage_and_no_retry(
    approved_limits, monkeypatch, kind, status, text, reason, code
):
    calls = sdk(monkeypatch, response(status, text, reason))
    gateway = build_agent_planner().gateway if kind == "planner" else build_relevance_gateway()
    with pytest.raises(StructuredResponseError) as caught:
        gateway.plan(
            system="system",
            user="user",
            schema={},
            **({"max_output_tokens": 100} if kind == "relevance" else {}),
        )
    assert len(calls) == 1
    assert caught.value.code == code and caught.value.usage["output_tokens"] == 10
    assert caught.value.raw_response["status"] == status
    assert gateway.last_model_receipt["response_received"]


class QuoteGateway:
    last_usage = {"input_tokens": 20, "output_tokens": 20}

    def __init__(self, quote, *, conflict=False):
        self.quote, self.conflict = quote, conflict

    def plan(self, **kwargs):
        request = json.loads(kwargs["user"])
        return {
            "request_sha256": request["request_sha256"],
            "decisions": [
                {
                    "candidate_id": c["candidate_id"],
                    "label": "relevant",
                    "source_field": "text",
                    "start": 999,
                    "end": 1000,
                    "quote": self.quote,
                    "constraints": {
                        k: "conflicts" if self.conflict and k == "negation" else "satisfied"
                        for k in CONSTRAINTS
                    },
                }
                for c in request["candidates"]
            ],
        }


def quote_judge(text, quote, **kwargs):
    return RelevanceAdapter(QuoteGateway(quote, **kwargs), input_rate="1", output_rate="1").judge(
        query="packaging damage", candidates=[{"fact_id": "dev", "text": text}], scope={}, complete=True
    )


def test_unique_exact_quote_derives_only_offsets_and_retains_original():
    result = quote_judge("配送及时。纸盒破损，充电正常。", "纸盒破损")
    assert result["complete"] and result["selected_ids"] == ["dev"]
    assert result["raw_decisions"][0]["start"] == 999
    assert result["decisions"][0]["start"] == 5
    assert result["span_derivations"][0]["reason"] == "unique_exact_quote_occurrence"


@pytest.mark.parametrize(
    "text,quote,conflict",
    [
        ("damage damage", "damage", False),
        ("damaged", "damage!", False),
        ("not damaged", "damaged", True),
        ("damaged", "", False),
    ],
)
def test_relocation_never_repairs_ambiguity_absence_or_semantic_conflict(text, quote, conflict):
    result = quote_judge(text, quote, conflict=conflict)
    assert not result["complete"] and result["selected_ids"] == []
    assert result["cost_known"] and result["tokens"] == 40


@pytest.mark.django_db(transaction=True)
def test_planner_received_failure_settles_shared_ledger_without_redispatch(
    approved_limits, metric_scope, monkeypatch, enable_workspace_model_budget
):
    from insight.investigation import graph, planner
    from insight.models import InvestigationRun

    calls = sdk(monkeypatch, response("incomplete", "{}", "max_output_tokens"))
    selected = planner.ModelPlanner()
    monkeypatch.setattr(planner, "planner_for", lambda provider=None: selected)
    users, shops = metric_scope
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    run = InvestigationRun.objects.create(
        shop=shops[0],
        requested_by=users["a"],
        child_asin="B000TEST01",
        query="Packaging evidence",
        max_steps=6,
        state={"planner_provider": "deepseek"},
    )
    result = graph.execute_investigation(run)
    entry = run.budget_ledger.get(kind="model")
    assert result.status == "partial" and len(calls) == 1
    assert entry.tokens == 30 and entry.cost > 0
    assert entry.metadata["failure_code"] == "output_truncated"
    assert entry.metadata["charge_outcome"] == "recorded_by_usage"
    graph.execute_investigation(InvestigationRun.objects.get(pk=run.pk))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "review_report",
    [
        [
            {"id": "DEV-NOMATCH", "text": "The carton arrived crushed."},
            {"id": "DEV-NEGATED", "text": "There was no packaging damage."},
            {"id": "DEV-ASSERTED", "text": "The packaging was damaged."},
        ]
    ],
    indirect=True,
)
@pytest.mark.django_db
def test_candidate_stage_counts_separate_lexical_context_and_judgment(review_report, monkeypatch, enable_workspace_model_budget):  # noqa: F811
    from insight.semantic import relevance, retrieval

    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "1")
    monkeypatch.setattr(
        relevance,
        "configured_adapter",
        lambda: RelevanceAdapter(QuoteGateway("The packaging was damaged."), input_rate="1", output_rate="1"),
    )
    result = retrieval.retrieve(
        actor=users["a"],
        shop=shops[0],
        asin="B000TEST01",
        query="packaging damage",
        evidence_context="asserted",
        start=date(2026, 9, 1),
        end=date(2026, 9, 2),
    )
    stage = result["candidate_stage"]
    assert stage["scoped_count"] == 3 and stage["lexical_matched_count"] == 2
    # Full small-scope admission does not erase the original lexical miss.
    assert stage["lexical_not_admitted_count"] == 1
    assert stage["context_filtered_count"] == 2 and stage["adjudication_input_count"] == 1
    assert result["retrieval_complete"]  # context exclusions do not make exhaustive scan incomplete
