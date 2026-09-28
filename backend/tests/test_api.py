from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_and_products():
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["products"] == 5

    products = client.get("/api/v1/products").json()["items"]
    assert len(products) == 5
    assert all(item["product_id"] for item in products)


def test_overview_uses_real_dataset():
    response = client.get("/api/v1/overview", params={"product_id": "B07P9S6R9Y"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["trend"]["points"]
    assert payload["trend"]["source_version"]
    assert len(payload["kpis"]) == 4


def test_anomaly_and_topic_endpoints():
    anomalies = client.get("/api/v1/anomalies").json()["items"]
    topic_payload = client.get("/api/v1/reviews/topics").json()
    assert anomalies
    assert topic_payload["items"] == []
    assert topic_payload["status"] == "pending_offline_enrichment"


def test_review_tool_api_uses_runtime_reviews_and_traceable_contract():
    search = client.post(
        "/api/v1/reviews/search",
        json={
            "request_id": "req-review-1",
            "product_id": "B07P9S6R9Y",
            "query": "charges",
            "top_k_final": 3,
        },
    )
    assert search.status_code == 200
    payload = search.json()
    assert payload["tool_name"] == "review_search_tool"
    assert payload["source_version"] == "amazon-reviews-2023-standardized-v1"
    assert payload["data"]["retrieval_backend"] == "keyword"
    assert payload["evidence_refs"]
    assert all(item["product_id"] == "B07P9S6R9Y" for item in payload["data"]["items"])

    detail = client.get(f'/api/v1/reviews/{payload["evidence_refs"][0]}')
    assert detail.status_code == 200
    assert detail.json()["tool_name"] == "review_detail_tool"
    assert detail.json()["data"]["review"]["review_id"] == payload["evidence_refs"][0]


def test_keyword_backend_is_explicit_default_and_has_no_embedding_metadata():
    response = client.post(
        "/api/v1/reviews/search",
        json={
            "request_id": "req-review-keyword",
            "product_id": "B07P9S6R9Y",
            "query": "charges",
            "retrieval_backend": "keyword",
            "top_k_final": 2,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["retrieval_backend"] == "keyword"
    assert data["retrieval_version"] == "keyword-v1"
    assert data["embedding_version"] is None
    assert data["rerank_mode"] == "never"


def test_metric_engine_contracts_and_anomaly_detection_api():
    compare = client.get(
        "/api/v1/metrics/compare",
        params={
            "product_id": "B07P9S6R9Y",
            "metric_name": "avg_daily_orders",
            "current_start": "2020-06-01",
            "current_end": "2020-06-30",
            "baseline_start": "2020-03-01",
            "baseline_end": "2020-05-31",
            "granularity": "month",
        },
    )
    assert compare.status_code == 200
    assert compare.json()["relative_change"] < -0.25
    assert compare.json()["data_source"] == "synthetic"

    detection = client.get(
        "/api/v1/anomalies/detect",
        params={
            "product_id": "B07P9S6R9Y",
            "metric_name": "refund_rate",
            "current_start": "2020-06-01",
            "current_end": "2020-06-30",
            "baseline_start": "2020-03-01",
            "baseline_end": "2020-05-31",
            "granularity": "month",
            "method": "auto",
        },
    )
    assert detection.status_code == 200
    payload = detection.json()
    assert payload["is_anomaly"] is True
    assert [item["method"] for item in payload["detections"]] == ["baseline_delta", "robust_zscore"]
    assert payload["reason"] == "退款率较近期基线显著恶化"


def test_multi_metric_analysis_api():
    response = client.get(
        "/api/v1/products/B07P9S6R9Y/multi-metric-analysis",
        params={"period": "2022-02"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["pattern"] == "growth_with_refund_risk"
    assert payload["business_direction"] == "mixed"
    assert payload["primary_signal"] == "refund_rate"
    assert payload["source"] == "deterministic"
