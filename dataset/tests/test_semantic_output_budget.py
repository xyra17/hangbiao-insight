"""Constructed response failures and product gateway configuration; no API calls."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
from app.semantic.factory import build_semantic_service
from app.semantic.model_gateway import DeepSeekResponsesGateway, SemanticResponseError
from app.semantic.parser import SemanticParseError
from app.semantic.schemas import ReviewInput


def service(response, **overrides):
    requests = []

    class Client:
        @property
        def responses(self):
            return self

        def create(self, **kwargs):
            requests.append(kwargs)
            return response

    config = PRIMARY_SEMANTIC_CONFIG.model_copy(
        update={
            "model": "deepseek-flash",
            "max_attempts": 3,
            "prompt_version": "semantic-prompt-v1.2",
            **overrides,
        }
    )
    gateway = DeepSeekResponsesGateway(
        config=config, client=Client(), sleep=lambda _: None
    )
    return build_semantic_service(
        ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json", gateway=gateway
    ), requests


def response(status="completed", text='{"claims":[]}', reason=None):
    result = SimpleNamespace(
        status=status,
        output_text=text,
        incomplete_details={"reason": reason} if reason else None,
        id="synthetic-response",
    )
    result.model_dump = lambda **kwargs: {
        "status": status,
        "output_text": text,
        "incomplete_details": result.incomplete_details,
        "id": result.id,
    }
    return result


@pytest.mark.parametrize(
    ("status", "text", "reason", "code"),
    [
        ("incomplete", "", "max_output_tokens", "output_truncated"),
        ("incomplete", '{"claims":[]}', "max_output_tokens", "output_truncated"),
        ("incomplete", "", "content_filter", "response_incomplete"),
        ("completed", "", None, "empty_output_text"),
        ("completed", " \n", None, "empty_output_text"),
        ("failed", '{"claims":[]}', None, "response_not_completed"),
    ],
)
def test_received_failure_never_retries_or_promotes(status, text, reason, code):
    instance, requests = service(
        response(status, text, reason), reasoning_effort="none", max_output_tokens=1200
    )
    with pytest.raises(SemanticResponseError) as exc:
        instance.extract(ReviewInput(review_id="id", product_id="p", text="works"))
    assert exc.value.code == code and exc.value.response_id == "synthetic-response"
    assert exc.value.raw_response["status"] == status and len(requests) == 1


@pytest.mark.parametrize("text", ["not json", '{"claims":', '{"claims":[],"extra":1}'])
def test_format_failures_remain_strict(text):
    instance, requests = service(
        response(text=text), reasoning_effort="none", max_output_tokens=1200
    )
    with pytest.raises(SemanticParseError):
        instance.extract(ReviewInput(review_id="id", product_id="p", text="works"))
    assert len(requests) == 1


def test_no_thinking_budget_and_original_guard():
    claims = [
        {
            "primary_topic": "function_performance",
            "secondary_topic": "general_performance",
            "polarity": "positive",
            "evidence_span": "invented",
        }
    ]
    instance, requests = service(
        response(text=json.dumps({"claims": claims})),
        reasoning_effort="none",
        max_output_tokens=1200,
    )
    result = instance.extract(ReviewInput(review_id="id", product_id="p", text="works"))
    assert result.rejected_count == 1 and result.accepted_count == 0
    assert (
        requests[0]["reasoning"] == {"effort": "none"}
        and requests[0]["max_output_tokens"] == 1200
    )
    assert "Claim-first but evidence-conservative" in requests[0]["input"][0]["content"]


def test_original_defaults_and_explicit_factory_controls(monkeypatch):
    from app.semantic import factory

    configs = []
    monkeypatch.setattr(
        factory,
        "DeepSeekResponsesGateway",
        lambda *, config: configs.append(config) or SimpleNamespace(config=config),
    )
    for key in ["SEMANTIC_REASONING_EFFORT", "SEMANTIC_MAX_OUTPUT_TOKENS"]:
        monkeypatch.delenv(key, raising=False)
    factory.build_semantic_gateway("deepseek")
    assert (
        configs[-1].reasoning_effort == "low" and configs[-1].max_output_tokens is None
    )
    monkeypatch.setenv("SEMANTIC_REASONING_EFFORT", "none")
    monkeypatch.setenv("SEMANTIC_MAX_OUTPUT_TOKENS", "1200")
    factory.build_semantic_gateway("deepseek")
    assert (
        configs[-1].reasoning_effort == "none" and configs[-1].max_output_tokens == 1200
    )
    instance, requests = service(response())
    instance.extract(ReviewInput(review_id="id", product_id="p", text="works"))
    assert (
        requests[0]["reasoning"] == {"effort": "low"}
        and "max_output_tokens" not in requests[0]
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"reasoning_effort": "disabled"},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": 1.5},
    ],
)
def test_unsupported_controls_rejected(overrides):
    with pytest.raises(ValueError):
        service(response(), **overrides)
