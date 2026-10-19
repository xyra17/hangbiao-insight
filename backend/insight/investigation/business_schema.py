"""Business decisions share field semantics with the frozen decision-contract scorer.

Tools and Job statuses are separate engineering facts, never business decisions.
No scenario-specific labels or frozen answers are loaded here.
"""
import json
import re

BUSINESS_LABEL_PATTERN = r"^[a-z][a-z0-9_]*$"

BUSINESS_SCHEMA_VERSION = "investigation-business-contract-v1"
BUSINESS_FIELDS = ("expected_route", "expected_behavior", "expected_next_step", "allowed_conclusion",
                   "forbidden_conclusion", "safety_escalation", "expected_stop_condition", "expected_tool_names")
FIELD_MEANINGS = {
    "expected_route": "business workflow selected, not a sequence of implementation tools",
    "expected_behavior": "evidence-bound decision behavior",
    "expected_next_step": "specific next business or evidence action",
    "allowed_conclusion": "statement supported by evidence and scope",
    "forbidden_conclusion": "statement not justified by this evidence",
    "safety_escalation": "specific safety handoff decision",
    "expected_stop_condition": "evidence or business boundary causing stop, not Job status",
    "expected_tool_names": "business capabilities required by the selected workflow",
}


def validate_business_value(field, value, *, version=None):
    from .business_contract_registry import VERSION, validate
    if version is None or version == VERSION:
        return validate(field, value)
    if version != BUSINESS_SCHEMA_VERSION:
        raise ValueError("unknown_business_contract_version")
    if not isinstance(value, str):
        raise ValueError("Business decision values must be semantic strings")
    if value in {"unknown", "pending_adjudication"}:
        return value
    if field == "expected_tool_names":
        items = json.loads(value)
        if not isinstance(items, list) or not items or not all(isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in items):
            raise ValueError("Business capabilities must be a nonempty JSON list of semantic names")
    elif not isinstance(value, str) or not re.fullmatch(BUSINESS_LABEL_PATTERN, value):
        raise ValueError("Business decision labels must use the shared semantic label format")
    return value


def parse_review_value(field, value, *, version):
    from .business_contract_registry import VERSION
    if version == VERSION and field == "expected_tool_names" and isinstance(value, str) and value != "unknown":
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("review_capability_array_required") from exc
    return validate_business_value(field, value, version=version)
