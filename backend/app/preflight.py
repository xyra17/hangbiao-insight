"""Local release preflight; intentionally performs no model calls."""
from __future__ import annotations

import os
from pathlib import Path

from .repositories.claim_store import ClaimStore
from .repositories.review_repository import REVIEW_PATH


def run() -> dict[str, object]:
    claim_store = ClaimStore()
    mode = os.getenv("EXECUTION_MODE") or ("external_planner" if os.getenv("DEEPSEEK_API_KEY") else "deterministic_local")
    return {
        "status": "ready",
        "execution_mode": mode,
        "planner_status": "deferred_external_model" if mode == "external_planner" and not os.getenv("DEEPSEEK_API_KEY") else ("local_deterministic" if mode == "deterministic_local" else "available"),
        "review_source": str(REVIEW_PATH),
        "review_source_exists": REVIEW_PATH.exists(),
        "claim_store": str(claim_store.path),
        "claim_store_exists": claim_store.path.exists(),
        "qdrant_configured": bool(os.getenv("QDRANT_URL") or os.getenv("QDRANT_PATH")),
        "external_model_called": False,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
