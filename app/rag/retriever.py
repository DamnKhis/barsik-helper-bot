"""Поиск релевантных фрагментов в Qdrant. Роль 2.

MVP: векторный поиск (COSINE) по коллекции с чанками.
TODO(роль 2, +баллы к критериям №1 и №2):
  - гибридный поиск: dense + BM25 (full-text индекс Qdrant) через RRF;
  - реранкер поверх топ-20 (bge-reranker-v2-m3 локально или LLM-реранк) —
    именно он даёт стабильный рост качества на подвохах;
  - расширение запроса синонимами («командировка» ~ «поездка»).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient

from indexer.embeddings import EmbeddingClient
from settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    text: str
    doc: str
    section_path: str
    link: str | None
    score: float


class Retriever:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.qdrant = AsyncQdrantClient(url=self.settings.qdrant_url)
        self.embeddings = EmbeddingClient(
            self.settings.embedding_endpoint, self.settings.embedding_key, self.settings.embedding_model
        )

    async def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        limit = top_k or self.settings.top_k
        try:
            vector = (await asyncio.to_thread(self.embeddings.embed_texts, [query]))[0]
            result = await self.qdrant.query_points(
                collection_name=self.settings.qdrant_collection,
                query=vector,
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001 — нет сети/коллекции: отвечаем честным «не знаю»
            logger.warning("Поиск недоступен (%s) — возвращаю пустой результат", exc)
            return []
        return [
            RetrievedChunk(
                text=point.payload.get("text", ""),
                doc=point.payload.get("doc", ""),
                section_path=point.payload.get("section_path", ""),
                link=point.payload.get("link") or None,
                score=float(point.score),
            )
            for point in result.points
        ]
