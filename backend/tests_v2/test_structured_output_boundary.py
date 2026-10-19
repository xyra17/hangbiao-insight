import json
from types import SimpleNamespace

import pytest

from app.semantic.structured_response import (
    StructuredOutputError,
    StructuredResponseError,
    dispatch,
    parse_structured_object,
    receive,
    validate_structured_object,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"decision": {"type": "string", "enum": ["stop", "clarify"]}},
    "required": ["decision"],
}


def test_accepts_raw_json_and_one_complete_json_fence():
    cases = [
        ('{"decision":"stop"}', "none"),
        ('  ```json\n{"decision":"stop"}\n```  ', "json_fence"),
        ('```\n{"decision":"clarify"}\n```', "json_fence"),
    ]
    for raw, wrapper in cases:
        value, normalized, metadata = parse_structured_object(raw)
        assert value == json.loads(normalized)
        assert metadata["wrapper"] == wrapper


@pytest.mark.parametrize(
    "raw,code",
    [
        ('Answer: {"decision":"stop"}', "structured_output_json_invalid"),
        ('{"decision":"stop"} done', "structured_output_json_invalid"),
        ('```json\n{"decision":"stop"}\n```\n```json\n{"decision":"stop"}\n```', "structured_output_fence_invalid"),
        ('```json\n{"decision":"stop"}', "structured_output_fence_invalid"),
        ('{"decision":"stop"', "structured_output_json_invalid"),
        ('null', "structured_output_object_required"),
        ('[]', "structured_output_object_required"),
        ('"stop"', "structured_output_object_required"),
    ],
)
def test_rejects_prose_multiple_blocks_truncation_and_non_objects(raw, code):
    with pytest.raises(StructuredOutputError) as caught:
        parse_structured_object(raw)
    assert caught.value.code == code


def test_rejects_schema_invalid_fields_without_repair():
    for raw in ('{"decision":"invented"}', '{"decision":"stop","extra":true}'):
        with pytest.raises(StructuredOutputError) as caught:
            validate_structured_object(raw, SCHEMA)
        assert caught.value.code == "structured_output_schema_invalid"


def response(text, *, status="completed"):
    raw = {
        "id": "controlled-response-id",
        "model": "deepseek-flash",
        "status": status,
        "output_text": text,
        "usage": {"input_tokens": 31, "output_tokens": 17},
    }
    return SimpleNamespace(
        id=raw["id"],
        model=raw["model"],
        status=status,
        output_text=text,
        usage=SimpleNamespace(
            input_tokens=31,
            output_tokens=17,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
        incomplete_details=None,
        model_dump=lambda **kwargs: raw,
    )


def request(schema=SCHEMA):
    return {
        "model": "deepseek-flash",
        "reasoning": {"effort": "none"},
        "max_output_tokens": 128,
        "text": {"format": {"type": "json_schema", "schema": schema}},
    }


def test_provider_boundary_keeps_raw_response_and_records_normalized_json():
    raw_text = '```json\n{ "decision" : "stop" }\n```'
    gateway = SimpleNamespace(last_usage={}, last_model_receipt={})
    normalized = receive(gateway, response(raw_text), request())

    assert normalized == '{"decision":"stop"}'
    assert gateway.last_model_receipt["raw_response"]["output_text"] == raw_text
    assert gateway.last_model_receipt["output_normalization"] == {
        "wrapper": "json_fence",
        "fence_stripped": True,
    }
    assert gateway.last_model_receipt["normalized_output_sha256"]


@pytest.mark.parametrize(
    "raw_text,code",
    [
        ('```json\n{"decision":"stop"}\n```\nextra', "structured_output_fence_invalid"),
        ('```json\n{"decision":"invalid"}\n```', "structured_output_schema_invalid"),
        ('```json\n{"decision":"stop"}\n```\n```json\n{}\n```', "structured_output_fence_invalid"),
    ],
)
def test_invalid_received_output_is_terminal_and_never_redispatched(monkeypatch, raw_text, code):
    calls = []
    value = response(raw_text)
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or value)
    )
    gateway = SimpleNamespace(last_usage={}, last_model_receipt={})

    with pytest.raises(StructuredResponseError) as caught:
        dispatch(gateway, client, request())

    assert caught.value.code == code
    assert caught.value.usage["output_tokens"] == 17
    assert caught.value.raw_response["output_text"] == raw_text
    assert gateway.last_model_receipt["response_received"] is True
    assert len(calls) == 1


def test_provider_refusal_or_empty_text_is_a_recorded_single_attempt():
    calls = []
    raw = response(" ")
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or raw)
    )
    gateway = SimpleNamespace(last_usage={}, last_model_receipt={})

    with pytest.raises(StructuredResponseError, match="empty_output_text") as caught:
        dispatch(gateway, client, request())

    assert len(calls) == 1
    assert caught.value.raw_response["output_text"] == " "


def test_planner_accepts_fenced_decision_but_rejects_tool_call_with_final_answer():
    from app.agent.planner import LLMPlanner
    from app.agent.state import InvestigationState

    decision = {
        "decision": "call_tool",
        "tool_name": "metric_snapshot",
        "arguments": "{}",
        "decision_summary": "先读取冻结指标。",
        "target_hypothesis_id": None,
        "stop_reason": None,
        "final_answer": None,
        "confidence": 0.7,
        "missing_fields": "",
        "counter_evidence_query": "",
    }

    class Gateway:
        execution_origin = "controlled_fixture"

        def __init__(self, payload):
            self.payload = payload
            self.calls = 0

        def plan(self, **kwargs):
            self.calls += 1
            return self.payload

    state = InvestigationState(request_id="parse-test", user_query="检查异常")
    gateway = Gateway("```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```")
    parsed = LLMPlanner(gateway).decide(state, ("metric_snapshot",))
    assert parsed.decision == "call_tool" and parsed.tool_name == "metric_snapshot"
    assert gateway.calls == 1

    mixed = {**decision, "final_answer": {"statement": "同时给出结论"}}
    gateway = Gateway(json.dumps(mixed, ensure_ascii=False))
    with pytest.raises(ValueError, match="invalid planner response"):
        LLMPlanner(gateway).decide(state, ("metric_snapshot",))
    assert gateway.calls == 1
