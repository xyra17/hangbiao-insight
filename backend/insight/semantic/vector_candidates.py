"""Immutable local embedding candidates. Similarity is never an evidence judgment.

Indexes contain only explicitly built, version-bound objects. Runtime intersects
with the authorized Django queryset before computing similarity. No remote URL,
runtime indexing, unbounded original-text scan, or lexical fallback is allowed.
"""

import hashlib
import ipaddress
import json
import math
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np

VERSION = "local-qwen3-candidates-v1"
QUERY_INSTRUCTION = "Given a customer feedback inquiry, retrieve original records relevant to the inquiry."
MAX_SCOPE = 50000
MAX_DOCUMENT_BYTES = 16000
RANK_WINDOW = 200


@lru_cache(maxsize=64)
def file_digest(path, size, modified, changed):
    # Immutable artifacts are re-hashed whenever their filesystem identity changes.
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_file_hash(path):
    stat = Path(path).stat()
    return file_digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def original_hash(row):
    return digest({"title": getattr(row, "title", ""), "text": row.text})


def index_scope(shop, asin, product_id, kind, manifest):
    return {"shop": str(shop.pk), "asin": asin, "product": str(product_id) if product_id else None,
            "kind": kind, "source_manifest_sha256": digest(manifest)}


class LocalEmbedding:
    """Use an already installed Qwen3 embedding model on a literal loopback IP."""

    def __init__(self):
        self.url = os.getenv("EVIDENCE_LOCAL_EMBEDDING_URL", "http://127.0.0.1:1234/v1")
        parsed = urlparse(self.url)
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        if not local or parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("embedding_requires_literal_loopback")
        self.model = os.environ["EVIDENCE_LOCAL_EMBEDDING_MODEL"]
        self.model_sha256 = os.environ["EVIDENCE_LOCAL_EMBEDDING_SHA256"]
        if "qwen3-embedding" not in self.model.lower() or len(self.model_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.model_sha256):
            raise ValueError("versioned_existing_embedding_required")
        if not Path(self.model).is_file() or verified_file_hash(self.model) != self.model_sha256:
            raise ValueError("existing_embedding_weights_hash_mismatch")
        self.dimension = 1024
        self.identity = {"version": VERSION, "model": self.model, "model_sha256": self.model_sha256,
                         "dimension": self.dimension, "query_instruction": QUERY_INSTRUCTION}

    def embed(self, text, *, query=False):
        if not text.strip() or len(text.encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("embedding_text_out_of_bound")
        value = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}" if query else text
        with httpx.Client(trust_env=False, timeout=30, follow_redirects=False) as client:
            secret = os.getenv("EVIDENCE_LOCAL_EMBEDDING_API_KEY", "local-no-auth")
            response = client.post(self.url.rstrip("/") + "/embeddings", json={"model": self.model, "input": value},
                                   headers={"Authorization": "Bearer " + secret})
            response.raise_for_status()
            vector = response.json()["data"][0]["embedding"]
        if len(vector) != self.dimension or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
            raise ValueError("embedding_vector_invalid")
        value = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(value)
        if norm <= 0:
            raise ValueError("embedding_zero_vector")
        return value / norm


def index_path(scope):
    directory = Path(os.environ["EVIDENCE_VECTOR_INDEX_DIR"]).resolve()
    return directory / (digest(scope) + ".sqlite3")


def build_index(rows, scope, *, start, end, provider=None):
    """Explicit preparation only. Fail atomically if any row cannot be indexed."""
    provider = provider or LocalEmbedding()
    path = index_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("immutable_index_already_exists")
    count = rows.count()
    if count > MAX_SCOPE:
        raise ValueError("embedding_scope_exceeds_bound")
    temporary = path.with_suffix(".building")
    if temporary.exists():
        raise ValueError("index_build_in_progress")
    records = []
    try:
        with sqlite3.connect(temporary) as database:
            database.execute("CREATE TABLE vectors (fact_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, vector BLOB NOT NULL)")
            database.execute("CREATE TABLE metadata (value TEXT NOT NULL)")
            for row in rows.order_by("id").iterator(chunk_size=100):
                vector = provider.embed(getattr(row, "title", "") + "\n" + row.text)
                content_hash = original_hash(row)
                database.execute("INSERT INTO vectors VALUES (?, ?, ?)", (str(row.pk), content_hash, np.asarray(vector, dtype=np.float32).tobytes()))
                records.append((str(row.pk), content_hash))
            metadata = {"scope": scope, "provider": provider.identity, "start": str(start), "end": str(end),
                        "row_count": count, "records_sha256": digest(records), "version": VERSION}
            database.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata, sort_keys=True),))
        temporary.rename(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {**metadata, "path": str(path), "index_sha256": verified_file_hash(path)}


def vector_candidates(rows, scope, *, query, start, end, provider=None):
    provider = provider or LocalEmbedding()
    path = index_path(scope)
    if not path.is_file():
        raise ValueError("embedding_index_missing")
    if rows.count() > MAX_SCOPE:
        raise ValueError("embedding_scope_exceeds_bound")
    # IDs originate only from the current authorized version/date/filter queryset.
    ids = sorted(str(pk) for pk in rows.values_list("pk", flat=True))
    scored, indexed = [], 0
    query_vector = provider.embed(query, query=True) if ids else None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        metadata = json.loads(database.execute("SELECT value FROM metadata").fetchone()[0])
        if metadata["scope"] != scope or metadata["provider"] != provider.identity or metadata["version"] != VERSION:
            raise ValueError("embedding_index_binding_mismatch")
        if not metadata["start"] <= str(start) <= str(end) <= metadata["end"]:
            raise ValueError("embedding_index_date_coverage_mismatch")
        for offset in range(0, len(ids), 400):
            group = ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in group)
            records = database.execute(f"SELECT fact_id, content_sha256, vector FROM vectors WHERE fact_id IN ({placeholders})", group).fetchall()
            indexed += len(records)
            for fact_id, content_hash, raw in records:
                vector = np.frombuffer(raw, dtype=np.float32)
                if len(vector) != provider.dimension or not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector)) - 1) > .001:
                    raise ValueError("embedding_index_vector_invalid")
                scored.append((fact_id, float(vector @ query_vector), content_hash))
    if indexed != len(ids):
        raise ValueError("embedding_index_coverage_incomplete")
    scored.sort(key=lambda item: (-item[1], item[0]))
    chosen = scored[:RANK_WINDOW]
    # Returned originals must still be byte-identical to the indexed immutable row.
    hashes = {pk: content_hash for pk, _, content_hash in chosen}
    for row in rows.filter(pk__in=list(hashes)).iterator():
        if original_hash(row) != hashes[str(row.pk)]:
            raise ValueError("embedding_index_original_mismatch")
    return [pk for pk, _, _ in chosen], {"status": "ready", "version": VERSION, "provider": provider.identity,
        "indexed_authorized_count": indexed, "rank_window": RANK_WINDOW, "rank_window_truncated": max(0, indexed - RANK_WINDOW),
        "index_sha256": verified_file_hash(path), "scope_sha256": digest(scope),
        "candidate_scores": [{"fact_id": pk, "similarity": score} for pk, score, _ in chosen]}


def fuse_candidates(lexical, semantic, limit):
    """RRF with a reserved semantic share; neither route alone exhausts the pool."""
    scores = {}
    for route in (lexical, semantic):
        for rank, pk in enumerate(dict.fromkeys(route), 1):
            scores[pk] = scores.get(pk, 0) + 1 / (60 + rank)
    reserved = semantic[:max(1, limit // 2)]
    ranked = sorted(scores, key=lambda pk: (-scores[pk], pk))
    selected = list(dict.fromkeys(reserved + ranked))[:limit]
    return sorted(selected, key=lambda pk: (-scores[pk], pk)), scores
