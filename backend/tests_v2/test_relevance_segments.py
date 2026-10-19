"""Server-addressed original evidence retains typos and rejects invented refs."""

import json

import pytest

from app.semantic.structured_response import validate_strict_schema
from insight.semantic.relevance import CONSTRAINTS, RelevanceAdapter, evidence_segments, segment_schema


class SegmentGateway:
    last_usage = {"input_tokens": 20, "output_tokens": 20}
    provenance = "controlled-segment-fixture"

    def __init__(self, mode="valid"):
        self.mode = mode
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        request = json.loads(kwargs["user"])
        segment_id = "title:all" if self.mode == "title" else "text:all"
        label = "unknown" if self.mode == "unknown" else "relevant"
        constraints = {key: "not_applicable" for key in CONSTRAINTS}
        if self.mode == "contradiction":
            constraints["topic"] = "unknown"
        decision = {"candidate_id": request["candidates"][0]["candidate_id"], "label": label,
                    "evidence_segment_id": None if label == "unknown" else segment_id,
                    "constraints": constraints}
        if self.mode == "invented":
            decision["evidence_segment_id"] = "text:invented"
        if self.mode == "fake_quote":
            decision["quote"] = "the cord falls out"
        if self.mode == "missing":
            return {"request_sha256": request["request_sha256"], "decisions": []}
        return {"request_sha256": request["request_sha256"], "decisions": [decision]}


def result(mode):
    return RelevanceAdapter(SegmentGateway(mode), input_rate="1", output_rate="1", protocol="segments").judge(
        query="compatibility problems", candidates=[{"fact_id": "record", "title": "cord falls out",
        "text": "the ciord falls out of it - returned"}], scope={"asin": "B0BYJ8ZVWH"}, complete=True)


def test_strict_segment_schema_and_original_typo_binding():
    validate_strict_schema(segment_schema())
    value = result("valid")
    assert value["selected_ids"] == ["record"]
    assert value["decisions"][0]["quote"] == "the ciord falls out of it - returned"
    assert value["schema_version"] == "evidence-relevance-segments-v1"
    assert value["segment_bindings"][0]["content_modified"] is False
    assert "quote" not in value["raw_decisions"][0]


@pytest.mark.parametrize("mode", ["invented", "fake_quote", "missing", "contradiction", "unknown"])
def test_failure_and_unknown_cannot_admit_evidence(mode):
    value = result(mode)
    assert not value["complete"] and value["selected_ids"] == []


def test_title_reference_cannot_be_silently_moved_to_body():
    value = result("title")
    assert value["decisions"][0]["source_field"] == "title"
    assert value["decisions"][0]["quote"] == "cord falls out"


def test_segments_preserve_sentence_context_and_unicode_offsets():
    row = {"title": "", "text": "  没有过热。\nThe ciord does not fit! It works elsewhere."}
    segments = evidence_segments(row)
    for segment in segments:
        quote = row[segment["source_field"]][segment["start"]:segment["end"]]
        assert quote.strip()
    assert any(row["text"][s["start"]:s["end"]] == "The ciord does not fit!" for s in segments)


def test_invalid_protocol_fails_before_dispatch():
    with pytest.raises(ValueError, match="invalid_relevance_protocol"):
        RelevanceAdapter(SegmentGateway(), protocol="invented")
