from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..checksums import write_checksums
from ..config import Settings
from ..embeddings import build_embedder
from ..embeddings.sparse import encode_sparse
from ..store.qdrant_store import QdrantStore


def run_eval(settings: Settings, golden: Path | None = None, top_k: int = 8) -> dict:
    golden = golden or settings.data_dir / "golden_retrieval.jsonl"
    manifest_path = settings.data_dir / "manifest.json"
    if not golden.exists():
        raise RuntimeError(f"Golden set not found: {golden}")
    if not manifest_path.exists():
        raise RuntimeError("No build manifest. Run ingest first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    collection = manifest["collection"]
    vocab = json.loads((settings.data_dir / "sparse_vocab.json").read_text(encoding="utf-8"))
    embedder = build_embedder(settings)
    store = QdrantStore(settings, collection)

    questions = []
    for line in golden.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            questions.append(json.loads(line))

    rows = []
    hits_at_1 = hits_at_3 = hits_at_5 = 0
    mrr_total = 0.0
    try:
        for item in questions:
            query = item["question"]
            dense = embedder.embed_query(query)
            sparse = encode_sparse(query, vocab)
            results = store.search_hybrid(dense, sparse, limit=top_k)
            relevant_rank = None
            for rank, result in enumerate(results, start=1):
                payload = result.get("payload") or {}
                if _is_relevant(payload, item):
                    relevant_rank = rank
                    break
            if relevant_rank:
                if relevant_rank == 1:
                    hits_at_1 += 1
                if relevant_rank <= 3:
                    hits_at_3 += 1
                if relevant_rank <= 5:
                    hits_at_5 += 1
                mrr_total += 1.0 / relevant_rank
            rows.append(
                {
                    "id": item.get("id", query[:40]),
                    "question": query,
                    "type": item.get("type", "text"),
                    "expected_doc_id": item.get("expected_doc_id", ""),
                    "rank": relevant_rank,
                    "top": [
                        {
                            "chunk_id": r.get("id"),
                            "score": round(float(r.get("score", 0.0)), 5),
                            "doc_id": (r.get("payload") or {}).get("doc_id"),
                            "section": (r.get("payload") or {}).get("section_path"),
                            "chunk_type": (r.get("payload") or {}).get("chunk_type"),
                        }
                        for r in results[:3]
                    ],
                }
            )
    finally:
        store.close()

    total = len(questions)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_id": manifest.get("build_id"),
        "collection": collection,
        "questions": total,
        "hit@1": round(hits_at_1 / total, 4) if total else 0.0,
        "hit@3": round(hits_at_3 / total, 4) if total else 0.0,
        "hit@5": round(hits_at_5 / total, 4) if total else 0.0,
        "mrr": round(mrr_total / total, 4) if total else 0.0,
        "rows": rows,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    (settings.data_dir / "eval_report.json").write_text(serialized, encoding="utf-8")
    build_dist = settings.dist_dir / str(manifest.get("build_id", ""))
    if build_dist.exists():
        (build_dist / "eval_report.json").write_text(serialized, encoding="utf-8")
        write_checksums(build_dist)
    return report


def _is_relevant(payload: dict, item: dict) -> bool:
    if item.get("expected_doc_id") and payload.get("doc_id") != item["expected_doc_id"]:
        return False
    section = " > ".join(payload.get("section_path") or []).lower()
    expected = item.get("expected_section_contains") or []
    if expected and not all(part.lower() in section for part in expected):
        return False
    expected_type = item.get("expected_chunk_type")
    if expected_type and payload.get("chunk_type") != expected_type:
        return False
    text = ((payload.get("raw_text") or "") + " " + (payload.get("enriched_text") or "")).lower()
    must = item.get("must_contain") or []
    return all(part.lower() in text for part in must)
