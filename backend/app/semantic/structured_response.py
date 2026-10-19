"""Bound structured Responses calls without retrying a received bad response."""

import hashlib
import json
import os
import re
from decimal import Decimal


def reasoning_effort(name):
    value = os.getenv(name, "low").strip()
    if value not in {"none", "low", "high", "max"}:
        raise ValueError("unsupported reasoning effort: " + name)
    return value


def structured_request(*, model, effort, name, system, user, schema, max_output_tokens):
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 6000:
        raise ValueError("invalid structured output limit")
    validate_strict_schema(schema)
    request = {
        "model": model,
        "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,
        "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
        "store": False,
    }
    request.update(sampling_options())
    return request


def sampling_options():
    temperature = os.getenv("STRUCTURED_RESPONSE_TEMPERATURE")
    if temperature is None:
        return {}
    value = float(temperature)
    if not 0 <= value <= 2:
        raise ValueError("invalid structured response temperature")
    return {"temperature": value}


def validate_strict_schema(schema):
    """Catch unsupported optional properties before a paid strict request."""
    if isinstance(schema, list):
        for item in schema:
            validate_strict_schema(item)
    elif isinstance(schema, dict):
        if isinstance(schema.get("type"), list) and set(schema["type"]) & {"object", "array"}:
            raise ValueError("strict_schema_complex_nullable_type_requires_anyof")
        if "properties" in schema:
            if set(schema.get("required", [])) != set(schema["properties"]) or schema.get("additionalProperties") is not False:
                raise ValueError("strict_schema_requires_all_properties_and_closed_objects")
        for value in schema.values():
            if isinstance(value, (dict, list)):
                validate_strict_schema(value)


def validate_wire_value(value, schema):
    """Validate the closed JSON-schema vocabulary used by our wire protocol."""
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                validate_wire_value(value, branch)
                return
            except ValueError:
                pass
        raise ValueError("wire_anyof_mismatch")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    checks = {"null": value is None, "object": isinstance(value, dict), "array": isinstance(value, list),
              "string": isinstance(value, str), "boolean": type(value) is bool,
              "integer": type(value) is int, "number": type(value) in {int, float}}
    if kinds and not any(checks.get(kind, False) for kind in kinds):
        raise ValueError("wire_type_mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("wire_enum_mismatch")
    if type(value) in {int, float}:
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            raise ValueError("wire_numeric_range_mismatch")
    if isinstance(value, str) and "pattern" in schema and re.search(schema["pattern"], value) is None:
        raise ValueError("wire_string_pattern_mismatch")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value) or (schema.get("additionalProperties") is False and set(value) - set(props)):
            raise ValueError("wire_object_fields_mismatch")
        for key in set(value) & set(props):
            validate_wire_value(value[key], props[key])
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"] or "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError("wire_array_length_mismatch")
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate_wire_value(item, schema["items"])


class StructuredOutputError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def parse_structured_object(text):
    """Parse one JSON object, optionally wrapped by one complete JSON fence.

    This deliberately does not search prose for braces, merge multiple blocks,
    or repair truncated/invalid output. The provider response remains untouched
    in its receipt; callers use the returned compact string as the normalized
    record.
    """
    if not isinstance(text, str):
        raise StructuredOutputError("structured_output_text_required")
    value_text = text.strip()
    if not value_text:
        raise StructuredOutputError("structured_output_json_invalid")

    fenced = False
    if value_text.startswith("```"):
        match = re.fullmatch(
            r"```(?P<language>json)?[ \t]*\r?\n(?P<body>.*?)\r?\n?```",
            value_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match or "```" in match.group("body"):
            raise StructuredOutputError("structured_output_fence_invalid")
        value_text = match.group("body").strip()
        fenced = True

    try:
        value = json.loads(value_text)
    except (TypeError, ValueError):
        raise StructuredOutputError("structured_output_json_invalid") from None
    if not isinstance(value, dict):
        raise StructuredOutputError("structured_output_object_required")
    normalized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value, normalized, {"wrapper": "json_fence" if fenced else "none", "fence_stripped": fenced}


def validate_structured_object(text, schema):
    value, normalized, metadata = parse_structured_object(text)
    try:
        validate_wire_value(value, schema)
    except ValueError as exc:
        raise StructuredOutputError("structured_output_schema_invalid", str(exc)) from None
    return value, normalized, metadata


def dispatch(gateway, client, request):
    """Preserve provider rejection receipts and make them terminal to the graph."""
    try:
        response = client.responses.create(**request)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        receipt = {"provider": "deepseek", "requested_model": request["model"],
                   "request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                   "reasoning_effort": request["reasoning"]["effort"], "max_output_tokens": request["max_output_tokens"],
                   "response_received": False, "http_status": status,
                   "provider_request_id": getattr(exc, "request_id", None),
                   "provider_error": getattr(exc, "body", None), "exception_type": type(exc).__name__,
                   "charge_outcome": "unknown_until_receipt"}
        gateway.last_model_receipt = receipt
        gateway.last_usage = {}
        raise StructuredResponseError("provider_request_rejected" if status is not None else "provider_transport_failure",
                                      receipt, {}, receipt.get("provider_error")) from exc
    return receive(gateway, response, request)


class StructuredResponseError(RuntimeError):
    def __init__(self, code, receipt, usage, raw_response):
        super().__init__(code)
        self.code, self.receipt, self.usage = code, receipt, usage
        self.raw_response = raw_response


def receive(gateway, response, request):
    usage = getattr(response, "usage", None)
    details = getattr(usage, "output_tokens_details", None)
    gateway.last_usage = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
    }
    raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else None
    status = getattr(response, "status", None)
    incomplete = getattr(response, "incomplete_details", None)
    reason = incomplete.get("reason") if isinstance(incomplete, dict) else getattr(incomplete, "reason", None)
    gateway.last_model_receipt = {
        "provider": "deepseek",
        "requested_model": request["model"],
        "reported_model": getattr(response, "model", None),
        "provider_response_id": getattr(response, "id", None),
        "reasoning_effort": request["reasoning"]["effort"],
        "max_output_tokens": request["max_output_tokens"],
        "request_sha256": hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "request_bytes": len(json.dumps(request, ensure_ascii=False).encode()),
        "response_received": True,
        "response_status": status,
        "incomplete_reason": reason,
        "version_pinned": False,
        "scope": "API receipt; model alias does not pin weights",
        "raw_response": raw,
    }
    code = (
        ("output_truncated" if reason == "max_output_tokens" else "response_incomplete")
        if status == "incomplete"
        else (
            "response_not_completed"
            if status != "completed"
            else "empty_output_text"
            if not isinstance(getattr(response, "output_text", None), str) or not response.output_text.strip()
            else None
        )
    )
    if code:
        gateway.last_model_receipt["failure_code"] = code
        raise StructuredResponseError(code, gateway.last_model_receipt, gateway.last_usage, raw)
    try:
        value, normalized, normalization = validate_structured_object(
            response.output_text, request["text"]["format"]["schema"]
        )
    except StructuredOutputError as exc:
        gateway.last_model_receipt["failure_code"] = exc.code
        if exc.detail:
            gateway.last_model_receipt["validation_error"] = exc.detail
        raise StructuredResponseError(exc.code, gateway.last_model_receipt, gateway.last_usage, raw) from None
    gateway.last_model_receipt["output_normalization"] = normalization
    gateway.last_model_receipt["normalized_output_sha256"] = hashlib.sha256(normalized.encode()).hexdigest()
    return normalized


def usage_cost(usage):
    """Received usage remains chargeable even when parsing or semantics fail."""
    values = [usage.get("input_tokens"), usage.get("output_tokens")]
    rates = [
        os.getenv("INVESTIGATION_INPUT_COST_PER_MILLION"),
        os.getenv("INVESTIGATION_OUTPUT_COST_PER_MILLION"),
    ]
    if any(type(v) is not int or v < 0 for v in values) or any(r is None for r in rates):
        return {"tokens": 0, "cost": "0", "cost_known": False}
    try:
        decimals = [Decimal(r) for r in rates]
        if any(not r.is_finite() or r < 0 for r in decimals):
            raise ValueError("invalid rates")
        return {
            "tokens": sum(values),
            "cost": str(sum(v * r for v, r in zip(values, decimals)) / 1_000_000),
            "cost_known": True,
        }
    except (ValueError, ArithmeticError):
        return {"tokens": sum(values), "cost": "0", "cost_known": False}
