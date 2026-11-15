from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from ..config import Settings
from ..models import Chunk


class QdrantStore:
    def __init__(self, settings: Settings, collection: str) -> None:
        self.settings = settings
        self.collection = collection
        if settings.qdrant_url:
            self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        else:
            settings.qdrant_path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(settings.qdrant_path))
        self.dim = settings.embedding_dim

    def recreate(self) -> None:
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config={"dense": VectorParams(size=self.dim, distance=Distance.COSINE)},
            sparse_vectors_config={"bm25": SparseVectorParams(modifier=Modifier.IDF)},
        )
        for field in ("doc_id", "chunk_type", "version", "tags"):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception:
                pass

    def upsert(self, chunks: list[Chunk], dense_vectors: list[list[float]], sparse_vectors: list[tuple[list[int], list[float]]]) -> None:
        points: list[PointStruct] = []
        for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors):
            indices, values = sparse
            points.append(
                PointStruct(
                    id=chunk.chunk_id,
                    vector={
                        "dense": dense,
                        "bm25": SparseVector(indices=indices, values=values),
                    },
                    payload=chunk.payload(),
                )
            )
        batch = 128
        for start in range(0, len(points), batch):
            self.client.upsert(collection_name=self.collection, points=points[start : start + batch], wait=True)

    def search_dense(self, vector: list[float], limit: int = 8, filters: dict | None = None) -> list[dict]:
        points = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            using="dense",
            limit=limit,
            with_payload=True,
            query_filter=_filter(filters),
        ).points
        return [{"id": p.id, "score": p.score, "payload": p.payload} for p in points]

    def search_sparse(self, indices: list[int], values: list[float], limit: int = 8, filters: dict | None = None) -> list[dict]:
        if not indices:
            return []
        points = self.client.query_points(
            collection_name=self.collection,
            query=SparseVector(indices=indices, values=values),
            using="bm25",
            limit=limit,
            with_payload=True,
            query_filter=_filter(filters),
        ).points
        return [{"id": p.id, "score": p.score, "payload": p.payload} for p in points]

    def search_hybrid(
        self,
        dense: list[float],
        sparse: tuple[list[int], list[float]],
        limit: int = 8,
        filters: dict | None = None,
        rrf_k: int = 60,
    ) -> list[dict]:
        pool = max(limit * 4, 20)
        dense_hits = self.search_dense(dense, pool, filters)
        sparse_hits = self.search_sparse(sparse[0], sparse[1], pool, filters)
        merged: dict[str, dict] = {}
        for rank, hit in enumerate(dense_hits, start=1):
            merged.setdefault(str(hit["id"]), hit)
            merged[str(hit["id"])]["rrf"] = merged[str(hit["id"])].get("rrf", 0.0) + 1.0 / (rrf_k + rank)
            merged[str(hit["id"])]["dense_score"] = hit["score"]
        for rank, hit in enumerate(sparse_hits, start=1):
            item = merged.setdefault(str(hit["id"]), hit)
            item["rrf"] = item.get("rrf", 0.0) + 1.0 / (rrf_k + rank)
            item["sparse_score"] = hit["score"]
        result = list(merged.values())
        result.sort(key=lambda item: item.get("rrf", 0.0), reverse=True)
        return result[:limit]

    def scroll_all(self) -> list[Any]:
        points: list[Any] = []
        offset = None
        while True:
            batch, offset = self.client.scroll(
                collection_name=self.collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            points.extend(batch)
            if offset is None:
                break
        return points

    def set_alias(self, alias: str, keep_previous: bool = False) -> None:
        try:
            result = self.client.get_aliases()
            items = getattr(result, "aliases", result)
            aliases = {item.alias_name: item.collection_name for item in items}
        except Exception:
            aliases = {}
        operations = []
        previous = aliases.get(alias)
        if previous and previous != self.collection:
            if keep_previous:
                if "bars_kb_prev" in aliases:
                    operations.append(
                        DeleteAliasOperation(delete_alias=DeleteAlias(alias_name="bars_kb_prev"))
                    )
                operations.append(
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=previous, alias_name="bars_kb_prev")
                    )
                )
            operations.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=alias)))
        operations.append(
            CreateAliasOperation(create_alias=CreateAlias(collection_name=self.collection, alias_name=alias))
        )
        try:
            self.client.update_collection_aliases(change_aliases_operations=operations)
        except Exception:
            pass

    def create_snapshot(self) -> str | None:
        try:
            result = self.client.create_snapshot(collection_name=self.collection)
            return getattr(result, "name", None)
        except Exception:
            return None

    def count(self) -> int:
        return self.client.count(collection_name=self.collection, exact=True).count

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


def _filter(filters: dict | None) -> Filter | None:
    if not filters:
        return None
    must = []
    for key, value in filters.items():
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                must.append(FieldCondition(key=key, match=MatchValue(value=item)))
        else:
            must.append(FieldCondition(key=key, match=MatchValue(value=value)))
    return Filter(must=must) if must else None


def vector_to_json(vector: Any) -> dict[str, Any]:
    if hasattr(vector, "indices"):
        return {"indices": list(vector.indices), "values": list(vector.values)}
    if isinstance(vector, list):
        return {"dense": vector}
    if isinstance(vector, dict):
        result: dict[str, Any] = {}
        for key, value in vector.items():
            if hasattr(value, "indices"):
                result[key] = {"indices": list(value.indices), "values": list(value.values)}
            else:
                result[key] = list(value)
        return result
    return {}
