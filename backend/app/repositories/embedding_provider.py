from __future__ import annotations
import os
from typing import Any

class LMStudioEmbeddingProvider:
    """Versioned OpenAI-compatible local embedding provider (Qwen3 Embedding 0.6B)."""
    version = "qwen3-embedding-0.6b-local-v1"
    dimension = 1024
    def __init__(self, client: Any | None = None, model: str | None = None):
        if client is None:
            from openai import OpenAI
            # Local LM Studio must not inherit a malformed corporate/proxy URL
            # (some environments expose ``NO_PROXY=::1`` which older httpx
            # versions parse incorrectly).  Build an explicit direct client.
            import httpx
            http_client = httpx.Client(trust_env=False)
            client = OpenAI(
                base_url=os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1"),
                api_key="local-no-auth",
                http_client=http_client,
            )
        # LM Studio exposes the imported GGUF under this API model id.  Keep
        # it as the default so a local install works without extra env wiring;
        # EMBEDDING_MODEL can still override it for another loaded model.
        self.client=client; self.model=model or os.getenv("EMBEDDING_MODEL", "text-embedding-qwen3-embedding-0.6b")
    def embed(self,text:str)->list[float]:
        data=self.client.embeddings.create(model=self.model,input=text).data[0].embedding
        if len(data)!=self.dimension: raise RuntimeError(f"embedding dimension mismatch: {len(data)} != {self.dimension}")
        return data
