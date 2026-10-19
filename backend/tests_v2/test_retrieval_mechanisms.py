import pytest

from tests_v2.test_semantic import review_report  # noqa: F401

DOCS = [
    {"id": "PACK", "text": "The packaging box was damaged and dented."},
    {"id": "ORDER", "text": "The order number is available; delivery was on time."},
    {"id": "SLOW", "text": "充电慢；the battery charges slowly overnight.", "language": "zh"},
    {"id": "DENIAL", "text": "No overheating was observed in normal use."},
    {"id": "HAZARD", "text": "The device overheated and produced a burning smell."},
    {"id": "QUESTION", "text": "Could the device overheat? This is a hypothetical question."},
    {"id": "PHOTO", "text": "The photos are clear and the product is blue."},
]


@pytest.mark.django_db
@pytest.mark.parametrize("review_report", [DOCS], indirect=True)
def test_retrieval_requires_concepts_and_preserves_unanswered_summary(review_report, client):  # noqa: F811
    users, shops = review_report
    client.force_authenticate(users["a"])

    def search(query):
        response = client.post("/api/v1/evidence-search/", {"shop": str(shops[0].pk), "child_asin": "B000TEST01",
                               "query": query, "start": "2026-09-01", "end": "2026-09-02"}, format="json")
        assert response.status_code == 200
        return response.data

    for query in ("Find packaging damage evidence", "查找包装受损记录"):
        assert [x["record_id"] for x in search(query)["items"]] == ["PACK"]
    assert [x["record_id"] for x in search("slow charging")["items"]] == ["SLOW"]
    for query in ("Find order unicorn telemetry", "Summarize lavender fragrance"):
        result = search(query)
        assert result["match_status"] == "no_hits" and result["items"] == []
    context = search("Summarize support records")
    assert context["match_status"] == "scope_context"
    assert context["relevant_hit_count"] == 0 and context["context_item_count"] > 0
    assert all(x["match_kind"] == "scope_context" for x in context["items"])
    assert "HAZARD" in {x["record_id"] for x in search("overheating")["items"]}
    assert "PHOTO" not in {x["record_id"] for x in search("hot")["items"]}
    assert [x["record_id"] for x in search("Check whether any review asserts overheating")["items"]] == ["HAZARD"]
    assert search("总结这条评论中的设备问题")["match_status"] == "scope_context"
    assert [x["record_id"] for x in search("请检查包装是否损坏")["items"]] == ["PACK"]
    assert search("overheating")["algorithm_manifest"]["algorithm_sha256"]
