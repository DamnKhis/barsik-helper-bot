from __future__ import annotations

from ..config import Settings
from ..providers.qwen import QwenEmbeddings
from .hash_embedder import HashEmbedder


def build_embedder(settings: Settings):
    if settings.embedding_provider == "qwen" and settings.qwen_api_key:
        return QwenEmbeddings(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            model=settings.qwen_embedding_model,
            dim=settings.embedding_dim,
        )
    return HashEmbedder(dim=settings.embedding_dim)
