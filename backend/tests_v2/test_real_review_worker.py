"""Real-review worker rejects invalid semantic outputs without paid dispatch."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def worker():
    path = Path(__file__).resolve().parents[2] / "scripts/public_agent_prediction.py"
    spec = importlib.util.spec_from_file_location("real_review_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rejected", [0, 1])
def test_semantic_contract_rejection_is_failure(rejected):
    value = {"predictions": [], "rejected_count": rejected}
    service = SimpleNamespace(extract=lambda review: SimpleNamespace(model_dump=lambda **kwargs: value))
    row = {"review_id": "example", "text": "It charges slowly."}
    if rejected:
        with pytest.raises(ValueError, match="semantic_output_contract_rejected"):
            worker().extract_once(service, row)
    else:
        assert worker().extract_once(service, row) == value
