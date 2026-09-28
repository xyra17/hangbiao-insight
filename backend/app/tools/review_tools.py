from __future__ import annotations

from time import perf_counter
from uuid import uuid4

from ..repositories.review_repository import REVIEW_SOURCE_VERSION, ReviewRepository
from .contracts import ToolRequest, ToolResult


class ReviewSearchTool:
    name = "review_search_tool"

    def __init__(self, repository: ReviewRepository | None = None) -> None:
        self.repository = repository or ReviewRepository()

    def execute(self, request: ToolRequest) -> ToolResult:
        started = perf_counter()
        params = request.params
        backend=str(params.get("retrieval_backend","keyword"))
        if backend in {"qdrant", "qdrant_rrf"}:
            try:
                from ..repositories.embedding_provider import LMStudioEmbeddingProvider
                from ..repositories.qdrant_retriever import QdrantReviewRetriever
                collection = params.get("qdrant_collection") or "product_review_embeddings_qwen3_v1"
                retriever = QdrantReviewRetriever(
                    collection=str(collection),
                    embedding_provider=LMStudioEmbeddingProvider(),
                )
                top_k = int(params.get("top_k_final", 8))
                points = retriever.query(str(params.get("query", "")), limit=max(top_k, 20), product_id=request.product_id, min_rating=params.get("min_rating"), max_rating=params.get("max_rating"))
                vector_items = [{"review_id": p.payload.get("review_id"), "product_id": p.payload.get("product_id"), "rating": p.payload.get("rating"), "score": float(p.score or 0)} for p in points]
                if backend == "qdrant_rrf":
                    keyword_items = self.repository.search(
                        query=str(params.get("query", "")), product_id=request.product_id,
                        start_date=request.time_range.start if request.time_range else None,
                        end_date=request.time_range.end if request.time_range else None,
                        min_rating=params.get("min_rating"), max_rating=params.get("max_rating"), limit=20,
                    )
                    ranks: dict[str, float] = {}
                    pool: dict[str, dict] = {}
                    for rank, item in enumerate(keyword_items, 1):
                        rid = item["review_id"]; ranks[rid] = ranks.get(rid, 0.0) + 1 / (60 + rank); pool[rid] = item
                    for rank, item in enumerate(vector_items, 1):
                        rid = item.get("review_id")
                        if not rid: continue
                        ranks[rid] = ranks.get(rid, 0.0) + 1 / (60 + rank); pool.setdefault(rid, item)
                    items = [pool[rid] | {"score": score} for rid, score in sorted(ranks.items(), key=lambda pair: pair[1], reverse=True)[:top_k]]
                else:
                    items = vector_items[:top_k]
                return ToolResult(tool_name=self.name, status="success" if items else "partial", data={"items": items, "retrieval_backend": backend, "rerank_mode": "rrf" if backend == "qdrant_rrf" else str(params.get("rerank_mode", "auto")), "query": str(params.get("query", "")), "embedding_version": retriever.embedding_provider.version, "retrieval_version": retriever.retrieval_version}, evidence_refs=[x["review_id"] for x in items if x.get("review_id")], warnings=[] if items else ["Qdrant 当前过滤条件下无结果"], source_version=REVIEW_SOURCE_VERSION, latency_ms=round((perf_counter()-started)*1000), trace_id=f"TR-{uuid4().hex}")
            except Exception as exc:
                return ToolResult(tool_name=self.name, status="error", data={"items": [], "retrieval_backend": backend, "error_type": type(exc).__name__}, evidence_refs=[], warnings=["Qdrant 后端不可用，未自动降级"], source_version=REVIEW_SOURCE_VERSION, latency_ms=round((perf_counter()-started)*1000), trace_id=f"TR-{uuid4().hex}")
        search=self.repository.hybrid_search if backend=="hybrid" else self.repository.search
        items = search(
            query=str(params.get("query", "")),
            product_id=request.product_id,
            start_date=request.time_range.start if request.time_range else None,
            end_date=request.time_range.end if request.time_range else None,
            min_rating=params.get("min_rating"),
            max_rating=params.get("max_rating"),
            limit=int(params.get("top_k_final", 8)),
        )
        embedding_version = "char-bigram-local-v1" if backend == "hybrid" else None
        retrieval_version = "hybrid-rrf-v1" if backend == "hybrid" else "keyword-v1"
        return ToolResult(
            tool_name=self.name,
            status="success" if items else "partial",
            data={"items": items, "retrieval_backend": backend, "rerank_mode": "never" if backend == "keyword" else str(params.get("rerank_mode","auto")), "query": str(params.get("query","")), "embedding_version": embedding_version, "retrieval_version": retrieval_version},
            evidence_refs=[item["review_id"] for item in items],
            warnings=[] if items else ["当前过滤条件下未检索到评论证据"],
            source_version=REVIEW_SOURCE_VERSION,
            latency_ms=round((perf_counter() - started) * 1000),
            trace_id=f"TR-{uuid4().hex}",
        )


class ReviewDetailTool:
    name = "review_detail_tool"

    def __init__(self, repository: ReviewRepository | None = None) -> None:
        self.repository = repository or ReviewRepository()

    def execute(self, request: ToolRequest) -> ToolResult:
        started = perf_counter()
        review_id = str(request.params.get("review_id", ""))
        item = self.repository.detail(review_id) if review_id else None
        return ToolResult(
            tool_name=self.name,
            status="success" if item else "error",
            data={"review": item},
            evidence_refs=[review_id] if item else [],
            warnings=[] if item else ["Review 不存在或 review_id 为空"],
            source_version=REVIEW_SOURCE_VERSION,
            latency_ms=round((perf_counter() - started) * 1000),
            trace_id=f"TR-{uuid4().hex}",
        )
