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

from export_paths import resolve_export_dir
from qdrant_client import QdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, default=None,
                        help="Build directory or dist/ directory; default: dist/LATEST")
    parser.add_argument("--qdrant-url", default="")
    parser.add_argument("--qdrant-path", default="data/qdrant-restored")
    parser.add_argument("--alias", default="bars_kb_current")
    parser.add_argument("--previous-alias", default="bars_kb_prev")
    args = parser.parse_args()

    export_dir = resolve_export_dir(args.export)
    config = json.loads((export_dir / "collection_config.json").read_text(encoding="utf-8"))
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
    with gzip.open(export_dir / "chunks.jsonl.gz", "rt", encoding="utf-8") as handle:
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
        result = client.get_aliases()
        items = getattr(result, "aliases", result)
        aliases = {item.alias_name: item.collection_name for item in items}
    except Exception:
        aliases = {}

    previous = aliases.get(args.alias)
    operations = []
    if previous and previous != collection:
        if args.previous_alias in aliases:
            operations.append(
                DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=args.previous_alias))
            )
        operations.append(
            CreateAliasOperation(
                create_alias=CreateAlias(collection_name=previous, alias_name=args.previous_alias)
            )
        )
    if args.alias in aliases:
        operations.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=args.alias)))
    operations.append(
        CreateAliasOperation(
            create_alias=CreateAlias(collection_name=collection, alias_name=args.alias)
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)
    if previous and previous != collection:
        print(f"previous alias: {args.previous_alias} -> {previous}")
    print(f"restored {total} points into {collection}; alias={args.alias}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
