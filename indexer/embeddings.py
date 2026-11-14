"""Клиент эмбеддингов. Роль 1.

OpenAI-совместимый API — подходит любой провайдер:
- облачный: text-embedding-3-small (1536) или GLM embedding;
- локальный: bge-m3 (1024) через Ollama (base_url=http://localhost:11434/v1)
  или vLLM. Для русского языка bge-m3 / multilingual-e5 — лучший бесплатный
  вариант, важнее качества самой LLM.
"""
from __future__ import annotations

import logging

from openai import OpenAI

logger = logging.getLogger(__name__)


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, batch_size: int = 64):
        self.client = OpenAI(base_url=base_url, api_key=api_key or "not-set")
        self.model = model
        self.batch_size = batch_size

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Возвращает векторы по порядку входа. Батчим — провайдеры режут длинные запросы."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            response = self.client.embeddings.create(model=self.model, input=batch)
            vectors.extend(item.embedding for item in response.data)
        return vectors
