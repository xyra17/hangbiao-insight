"""Cross-stage input freezing for resumable investigations.

An investigation is a reproducible read of one source snapshot.  Later
imports may create newer facts, but they must never silently replace the
inputs of a running or resumable investigation.  The graph stores this
contract in ``InvestigationRun.input_versions`` and calls the validators below
at every node boundary.
"""

from __future__ import annotations

from typing import Any

from insight.ingestion.contracts import digest

REQUIRED_KEYS = (
    "source_manifest",
    "metric_snapshot",
    "semantic_run",
    "mapping_versions",
    "rule_version",
    "workflow_version",
)


def input_fingerprint(input_versions: dict[str, Any]) -> str:
    return digest({key: input_versions.get(key) for key in REQUIRED_KEYS})


def validate_frozen_inputs(input_versions: dict[str, Any]) -> None:
    if not isinstance(input_versions, dict) or "source_manifest" not in input_versions:
        raise RuntimeError("investigation_input_not_frozen")
    missing = [key for key in REQUIRED_KEYS if key not in input_versions]
    if missing:
        raise RuntimeError(f"investigation_input_incomplete:{','.join(missing)}")
    manifest = input_versions["source_manifest"]
    if not isinstance(manifest, dict) or any(
        not isinstance(value, dict) or "kind" not in value or "revision" not in value
        for value in manifest.values()
    ):
        raise RuntimeError("investigation_source_manifest_invalid")


def assert_same_inputs(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    validate_frozen_inputs(expected)
    validate_frozen_inputs(actual)
    if input_fingerprint(expected) != input_fingerprint(actual):
        raise RuntimeError("investigation_input_changed")
