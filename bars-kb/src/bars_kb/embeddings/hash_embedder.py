from __future__ import annotations

import hashlib
import math
import re

TOKEN = re.compile(r"[a-zа-яё0-9]+", re.I)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text)]


class HashEmbedder:
    """Deterministic lexical hashing embedder for no-key mode.

    It is not a semantic model, but it gives stable, comparable vectors and
    enough lexical overlap for smoke tests and retrieval evaluation. Real
    builds use a Qwen embedding model (qwen/qwen3-embedding-8b by default).
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = tokenize(text)
        features: list[str] = []
        for token in tokens:
            features.append(token)
            if len(token) >= 4:
                features.append("p:" + token[:5])
                features.append("s:" + token[-4:])
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm:
            vector = [v / norm for v in vector]
        return vector
