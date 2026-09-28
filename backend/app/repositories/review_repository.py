from __future__ import annotations

import csv
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
REVIEW_PATH = PROJECT_ROOT / "dataset" / "data" / "01_标准化评论数据" / "评论数据集_v1.csv"
REVIEW_SOURCE_VERSION = "amazon-reviews-2023-standardized-v1"


@lru_cache(maxsize=1)
def _rows() -> tuple[dict[str, str], ...]:
    with REVIEW_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return tuple(csv.DictReader(handle))


def _review_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


class ReviewRepository:
    """Runtime Review source. Golden labels and Golden split files are never read here."""

    def detail(self, review_id: str) -> dict[str, Any] | None:
        row = next((item for item in _rows() if item["review_id"] == review_id), None)
        return self._public(row) if row else None

    def search(
        self,
        *,
        query: str = "",
        product_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        min_rating: float | None = None,
        max_rating: float | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        tokens = tuple(token.casefold() for token in query.split() if token.strip())
        found: list[tuple[int, str, dict[str, Any]]] = []
        for row in _rows():
            if product_id and row["sku"] != product_id:
                continue
            created = _review_date(row.get("created_at", ""))
            if start_date and (not created or created < start_date):
                continue
            if end_date and (not created or created > end_date):
                continue
            rating = float(row.get("rating") or 0)
            if min_rating is not None and rating < min_rating:
                continue
            if max_rating is not None and rating > max_rating:
                continue
            title = row.get("title", "").casefold()
            text = row.get("text", "").casefold()
            score = sum(3 for token in tokens if token in title) + sum(
                1 for token in tokens if token in text
            )
            if tokens and score == 0:
                continue
            found.append((score, row.get("created_at", ""), self._public(row)))
        found.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] | {"score": item[0]} for item in found[: max(1, min(limit, 20))]]

    def hybrid_search(self, *, query: str, product_id: str | None = None, start_date=None, end_date=None, min_rating=None, max_rating=None, limit: int = 8):
        keyword=self.search(query=query,product_id=product_id,start_date=start_date,end_date=end_date,min_rating=min_rating,max_rating=max_rating,limit=20)
        grams={query.casefold()[i:i+2] for i in range(max(0,len(query)-1))}
        vector=[]
        for row in self.search(query='',product_id=product_id,start_date=start_date,end_date=end_date,min_rating=min_rating,max_rating=max_rating,limit=20):
            text=(row['title']+' '+row['text']).casefold(); tg={text[i:i+2] for i in range(max(0,len(text)-1))}; score=len(grams & tg)/(len(grams|tg) or 1); vector.append((score,row))
        vector.sort(key=lambda x:x[0],reverse=True); ranks={}
        for seq in (keyword,[x[1] for x in vector]):
            for rank,item in enumerate(seq,1): ranks[item['review_id']]=ranks.get(item['review_id'],0)+1/(60+rank)
        pool={x['review_id']:x for x in keyword};pool.update({x[1]['review_id']:x[1] for x in vector})
        return [pool[k]|{'score':v} for k,v in sorted(ranks.items(),key=lambda x:x[1],reverse=True)[:limit]]

    @staticmethod
    def _public(row: dict[str, str]) -> dict[str, Any]:
        return {
            "review_id": row["review_id"],
            "product_id": row["sku"],
            "product_name": row.get("product_name"),
            "rating": float(row.get("rating") or 0),
            "title": row.get("title") or "无标题",
            "text": row.get("text") or "",
            "created_at": row.get("created_at") or None,
            "verified_purchase": row.get("verified_purchase", "").lower() == "true",
            "source": row.get("source") or "amazon_reviews_2023",
        }
