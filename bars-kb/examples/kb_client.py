"""Minimal retrieval client for the handed-over BARS KB.

Works with a Qdrant server or an embedded local Qdrant folder.
The query must be embedded with the same model that produced the collection.
Set QWEN_API_KEY and EMBEDDING_PROVIDER=qwen for the real collection;
hash-lexical is only for the no-key smoke build.

This client only retrieves chunks. Answer generation is the bot's job and
uses qwen/qwen3.8-flash with the same Qwen API key.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import SparseVector

TOKEN = re.compile(r"[a-zа-яё0-9]+", re.I)
ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text)]


def hash_embed(text: str, dim: int = 1024) -> list[float]:
    vector = [0.0] * dim
    features: list[str] = []
    for token in tokenize(text):
        features.append(token)
        if len(token) >= 4:
            features.append("p:" + token[:5])
            features.append("s:" + token[-4:])
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % dim
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def qwen_embed(text: str, dim: int = 1024) -> list[float]:
    api_key = os.getenv("QWEN_API_KEY", "")
    base_url = os.getenv("QWEN_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.getenv("QWEN_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
    if not api_key:
        raise RuntimeError("QWEN_API_KEY is not set; use EMBEDDING_PROVIDER=hash for the mock build")
    body = json.dumps(
        {"model": model, "input": [text], "dimensions": dim, "encoding_format": "float"},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/embeddings",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["data"][0]["embedding"]


def embed_query(text: str, dim: int = 1024) -> list[float]:
    provider = os.getenv("EMBEDDING_PROVIDER", "hash")
    return qwen_embed(text, dim) if provider == "qwen" else hash_embed(text, dim)


def encode_sparse(text: str, vocab: dict[str, int]) -> SparseVector:
    counts: dict[str, int] = {}
    for token in tokenize(text):
        if token in vocab:
            counts[token] = counts.get(token, 0) + 1
    items = sorted((vocab[token], count) for token, count in counts.items())
    return SparseVector(
        indices=[index for index, _ in items],
        values=[1.0 + math.log(count) for _, count in items],
    )


class KB:
    def __init__(self, export_dir: Path | None = None) -> None:
        load_env()
        self.export_dir = Path(export_dir or os.getenv("KB_EXPORT_DIR", ROOT / "dist"))
        if not (self.export_dir / "collection_config.json").exists():
            candidates = sorted(p for p in self.export_dir.iterdir() if (p / "collection_config.json").exists())
            if not candidates:
                raise RuntimeError(f"Export dir not found: {self.export_dir}")
            self.export_dir = candidates[-1]
        if not self.export_dir.exists():
            raise RuntimeError(f"Export dir not found: {self.export_dir}")
        config = json.loads((self.export_dir / "collection_config.json").read_text(encoding="utf-8"))
        self.collection = config.get("alias") or config["collection"]
        self.dim = int(config.get("embedding", {}).get("dim", 1024))
        self.vocab = json.loads((self.export_dir / "sparse_vocab.json").read_text(encoding="utf-8"))
        qdrant_url = os.getenv("QDRANT_URL", "")
        if qdrant_url:
            self.client = QdrantClient(url=qdrant_url, api_key=os.getenv("QDRANT_API_KEY") or None)
        else:
            self.client = QdrantClient(path=os.getenv("QDRANT_PATH", str(ROOT / "data/qdrant")))

    def search(self, query: str, top_k: int = 8, mode: str = "hybrid") -> list[dict]:
        dense = embed_query(query, self.dim)
        sparse = encode_sparse(query, self.vocab)
        pool = max(top_k * 4, 20)
        dense_hits = self._dense(dense, pool)
        sparse_hits = self._sparse(sparse, pool) if sparse.indices else []
        if mode == "dense":
            return dense_hits[:top_k]
        if mode == "sparse":
            return sparse_hits[:top_k]
        merged: dict[str, dict] = {}
        for rank, hit in enumerate(dense_hits, start=1):
            merged[hit["id"]] = hit
            merged[hit["id"]]["rrf"] = 1.0 / (60 + rank)
        for rank, hit in enumerate(sparse_hits, start=1):
            item = merged.setdefault(hit["id"], hit)
            item["rrf"] = item.get("rrf", 0.0) + 1.0 / (60 + rank)
        result = sorted(merged.values(), key=lambda item: item.get("rrf", 0.0), reverse=True)
        return result[:top_k]

    def _dense(self, vector: list[float], limit: int) -> list[dict]:
        points = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            using="dense",
            limit=limit,
            with_payload=True,
        ).points
        return [{"id": str(p.id), "score": p.score, "payload": p.payload} for p in points]

    def _sparse(self, vector: SparseVector, limit: int) -> list[dict]:
        points = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            using="bm25",
            limit=limit,
            with_payload=True,
        ).points
        return [{"id": str(p.id), "score": p.score, "payload": p.payload} for p in points]
