from app.evidence import EvidenceGuard, EvidenceInput, EvidenceStatus, SourceType
from app.tools import EvidenceGuardTool, ToolRequest


class Reviews:
    def detail(self, review_id):
        if review_id != "r1":
            return None
        return {"title": "Stops charging", "text": "It stops charging randomly after a few minutes."}


def guard():
    return EvidenceGuard(Reviews())


def test_valid_review_quote_passes_and_duplicate_is_removed():
    item = EvidenceInput(source_type="review", source_id="r1", quote="stops charging randomly")
    result = guard().validate(claim="评论出现间歇性断充反馈", evidence=[item, item], allowed_source_refs={(SourceType.REVIEW, "r1")})
    assert result.passed is True
    assert result.evidence_status == EvidenceStatus.VERIFIED
    assert len(result.validated_evidence) == 1
    assert result.warnings == ["duplicate_evidence:r1"]


def test_missing_quote_unknown_review_and_empty_evidence_are_rejected():
    quote = EvidenceInput(source_type="review", source_id="r1", quote="invented quote")
    missing = EvidenceInput(source_type="review", source_id="missing", quote="anything")
    quote_result = guard().validate(claim="观察", evidence=[quote], allowed_source_refs={(SourceType.REVIEW, "r1")})
    missing_result = guard().validate(claim="观察", evidence=[missing], allowed_source_refs={(SourceType.REVIEW, "missing")})
    empty_result = guard().validate(claim="观察", evidence=[], allowed_source_refs=set())
    assert "quote_not_found:r1" in quote_result.errors
    assert "review_not_found:missing" in missing_result.errors
    assert empty_result.errors == ["evidence_required"]


def test_source_must_have_been_returned_by_tool_and_types_are_distinct():
    review = EvidenceInput(source_type="review", source_id="r1", quote="stops charging randomly")
    metric = EvidenceInput(source_type="metric", source_id="m1", quote="退款率上升 2.1pp")
    rejected = guard().validate(claim="观察", evidence=[review], allowed_source_refs=set())
    accepted = guard().validate(claim="经营指标事实", evidence=[metric], allowed_source_refs={(SourceType.METRIC, "m1")})
    assert rejected.passed is False
    assert any(error.startswith("source_not_returned_by_tool") for error in rejected.errors)
    assert accepted.passed is True
    assert accepted.validated_evidence[0].source_type == SourceType.METRIC


def test_guard_does_not_promote_review_correlation_to_causality_and_tracks_conflict():
    causal = guard().validate(
        claim="退款率上升就是因为间歇性断充导致",
        evidence=[EvidenceInput(source_type="review", source_id="r1", quote="stops charging randomly")],
        allowed_source_refs={(SourceType.REVIEW, "r1")},
    )
    conflict = guard().validate(
        claim="评论信号出现变化",
        evidence=[EvidenceInput(source_type="metric", source_id="m1", quote="退款率未变化", stance="contradict")],
        allowed_source_refs={(SourceType.METRIC, "m1")},
    )
    assert causal.passed is False
    assert "causality_not_proven_by_evidence_guard" in causal.warnings
    assert conflict.evidence_status == EvidenceStatus.CONFLICT


def test_evidence_guard_tool_returns_standard_tool_contract():
    tool = EvidenceGuardTool(guard())
    result = tool.execute(
        ToolRequest(
            request_id="req-1",
            product_id="p1",
            params={
                "claim": "评论出现断充反馈",
                "evidence": [{"source_type": "review", "source_id": "r1", "quote": "stops charging randomly"}],
                "allowed_source_refs": [{"source_type": "review", "source_id": "r1"}],
            },
        )
    )
    assert result.tool_name == "evidence_guard_tool"
    assert result.status == "success"
    assert result.data["evidence_status"] == "verified"
    assert result.source_version == "evidence-guard-v1"
