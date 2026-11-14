"""Qdrant: коллекция, детерминированные id, upsert, удаление по документу. Роль 1.

id чанка = uuid5 от (документ, раздел, номер) — повторная индексация того же
документа ПЕРЕЗАПИСЫВАЕТ точки, а не плодит дубли. Поле payload `doc`
проиндексировано как KEYWORD — удаление/фильтрация по документу быстрая.
"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient, models

from indexer.chunking import Chunk


def get_client(url: str) -> QdrantClient:
    return QdrantClient(url=url, timeout=10)


def ensure_collection(client: QdrantClient, collection: str, dim: int) -> None:
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        client.create_payload_index(
            collection_name=collection,
            field_name="doc",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


def _point_id(chunk: Chunk, index: int) -> str:
    key = f"{chunk.doc}::{chunk.section_path}::{index}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def upsert_chunks(
    client: QdrantClient,
    collection: str,
    chunks: list[Chunk],
    vectors: list[list[float]],
) -> int:
    points = [
        models.PointStruct(id=_point_id(chunk, i), vector=vector, payload=chunk.payload())
        for i, (chunk, vector) in enumerate(zip(chunks, vectors), start=1)
    ]
    if points:
        client.upsert(collection_name=collection, points=points, wait=True)
    return len(points)


def delete_doc(client: QdrantClient, collection: str, doc: str) -> None:
    """Удаляет все точки одного документа — основа идемпотентной реиндексации."""
    client.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="doc", match=models.MatchValue(value=doc))]
            )
        ),
    )
