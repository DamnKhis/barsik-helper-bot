from __future__ import annotations

import math
from collections import Counter

from .hash_embedder import tokenize


def build_vocab(texts: list[str]) -> dict[str, int]:
    tokens: set[str] = set()
    for text in texts:
        tokens.update(tokenize(text))
    return {token: index for index, token in enumerate(sorted(tokens))}


def encode_sparse(text: str, vocab: dict[str, int]) -> tuple[list[int], list[float]]:
    counts = Counter(token for token in tokenize(text) if token in vocab)
    if not counts:
        return [], []
    items = sorted((vocab[token], count) for token, count in counts.items())
    indices = [index for index, _ in items]
    values = [1.0 + math.log(count) for _, count in items]
    return indices, values
