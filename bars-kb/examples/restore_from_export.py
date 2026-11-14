"""Restore a handed-over export into a Qdrant collection.

Usage:
    python examples/restore_from_export.py --export dist/<build> --qdrant-url http://127.0.0.1:6333

If --qdrant-url is omitted, an embedded Qdrant folder is created at QDRANT_PATH.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    Distance,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--qdrant-url", default="")
    parser.add_argument("--qdrant-path", default="data/qdrant-restored")
    parser.add_argument("--alias", default="bars_kb_current")
    args = parser.parse_args()

    config = json.loads((args.export / "collection_config.json").read_text(encoding="utf-8"))
    collection = config["collection"]
    dim = int(config["embedding"]["dim"])
    client = (
        QdrantClient(url=args.qdrant_url)
        if args.qdrant_url
        else QdrantClient(path=args.qdrant_path)
    )

    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=dim, distance=Distance.COSINE)},
        sparse_vectors_config={"bm25": SparseVectorParams(modifier="idf")},
    )

    batch: list[PointStruct] = []
    total = 0
    with gzip.open(args.export / "chunks.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            sparse = row.get("sparse_vector") or {}
            batch.append(
                PointStruct(
                    id=row["id"],
                    vector={
                        "dense": row["dense_vector"],
                        "bm25": SparseVector(
                            indices=sparse.get("indices", []),
                            values=sparse.get("values", []),
                        ),
                    },
                    payload=row["payload"],
                )
            )
            total += 1
            if len(batch) >= 256:
                client.upsert(collection_name=collection, points=batch, wait=True)
                batch = []
    if batch:
        client.upsert(collection_name=collection, points=batch, wait=True)

    try:
        client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(collection_name=collection, alias_name=args.alias)
                )
            ]
        )
    except Exception:
        pass
    print(f"restored {total} points into {collection}; alias={args.alias}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
