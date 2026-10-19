"""Partial real runs cannot be mistaken for the planned whole quality round."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("counts", [(0, 5, 0), (48, 29, 300), (47, 30, 300), (48, 30, 299), (48, 30, 300)])
def test_whole_coverage_is_required_before_full_scoring(tmp_path, monkeypatch, counts):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("unified_coverage_test", scripts / "unified_quality_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    investigations, retrievals, classifications = counts
    for name, value in {
        "runtime.json": {"trajectories": [{}] * investigations},
        "retrieval-runtime.json": {"per_query": {str(i): {} for i in range(retrievals)}},
        "raw-predictions.json": {"predictions": [{}] * classifications},
    }.items():
        (tmp_path / name).write_text(json.dumps(value))
    if counts == (48, 30, 300):
        runtime, retrieval = module.validate_full_runtime_coverage(tmp_path)
        assert len(runtime["trajectories"]) == 48 and len(retrieval["per_query"]) == 30
    else:
        with pytest.raises(ValueError, match="partial runtime cannot receive full-coverage scoring"):
            module.validate_full_runtime_coverage(tmp_path)
    assert not (tmp_path / "offline-evaluation").exists()
