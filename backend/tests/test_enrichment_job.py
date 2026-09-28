from app.semantic.enrichment_job import _blocked_status, _gateway_failure
from app.semantic.model_gateway import ModelGatewayError


def test_gateway_errors_are_classified_for_circuit_breaker():
    error = ModelGatewayError("deepseek request failed after 3 attempts", attempts=3)
    assert _gateway_failure(error) is True
    assert _blocked_status(error) == "blocked_model_gateway_error"


def test_missing_key_and_quota_messages_are_blocking_without_model_calls():
    missing = RuntimeError("DEEPSEEK_API_KEY is required")
    quota = RuntimeError("insufficient balance or quota")
    assert _gateway_failure(missing) is True
    assert _blocked_status(missing) == "blocked_missing_deepseek_key"
    assert _gateway_failure(quota) is True
    assert _blocked_status(quota) == "blocked_model_gateway_error"


def test_unrelated_data_error_does_not_trip_gateway_breaker():
    error = ValueError("invalid review rating")
    assert _gateway_failure(error) is False
