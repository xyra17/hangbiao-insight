import pytest

from insight.investigation.freeze import assert_same_inputs, input_fingerprint, validate_frozen_inputs


def frozen(revision=1):
    return {
        "source_manifest": {"source-id": {"kind": "reviews", "revision": revision}},
        "metric_snapshot": "metric-id",
        "semantic_run": "semantic-id",
        "mapping_versions": {"product-id": 2},
        "rule_version": "rules-v1",
        "workflow_version": "investigation-fixed-v2",
    }


def test_frozen_input_fingerprint_rejects_cross_stage_revision_change():
    first = frozen()
    second = frozen(2)
    validate_frozen_inputs(first)
    assert input_fingerprint(first) != input_fingerprint(second)
    with pytest.raises(RuntimeError, match="input_changed"):
        assert_same_inputs(first, second)


def test_frozen_input_requires_manifest_and_workflow_contract():
    with pytest.raises(RuntimeError, match="input_incomplete"):
        validate_frozen_inputs({"source_manifest": {}})

