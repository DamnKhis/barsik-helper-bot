"""Re-embed a handed-over export with another embedding model.

The export contains raw/enriched text and sparse BM25 vectors, but dense
vectors are model-specific. If the next developer uses a different embedding
model, this script recomputes only the dense vectors and produces a new
portable export.

Examples:
    # OpenAI-compatible endpoint (OpenRouter/DashScope, OpenAI, vLLM, Ollama...)
    python examples/reembed_from_export.py \
      --export dist/<build> --provider openai \
      --base-url https://openrouter.ai/api/v1 \
      --api-key $QWEN_API_KEY --model qwen/qwen3-embedding-8b --dim 1024

    # Local sentence-transformers model (run inside the target environment)
    python examples/reembed_from_export.py \
      --export dist/<build> --provider local --model BAAI/bge-m3 --dim 1024

The output directory keeps the same chunk ids, payloads and sparse vectors,
so it can be restored with examples/restore_from_export.py.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import urllib.request
from pathlib import Path

from export_paths import resolve_export_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, default=None,
                        help="Build directory or dist/ directory; default: dist/LATEST")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--provider", choices=["openai", "local"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dim", type=int, default=0, help="0 = provider/model default")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--send-dimensions", action="store_true",
                        help="Send the dimensions field (OpenAI text-embedding-3, Qwen v4)")
    args = parser.parse_args()

    export_dir = resolve_export_dir(args.export)
    config = json.loads((export_dir / "collection_config.json").read_text(encoding="utf-8"))
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    records = _read_chunks(export_dir / "chunks.jsonl.gz")
    if not records:
        raise RuntimeError("No chunks in export")

    texts = [((row.get("payload") or {}).get("enriched_text") or "") for row in records]
    print(f"re-embedding {len(texts)} chunks with {args.provider}:{args.model}")

    if args.provider == "local":
        vectors = _embed_local(texts, args.model, args.batch_size)
        dim = len(vectors[0])
    else:
        if not args.base_url:
            raise RuntimeError("--base-url is required for provider=openai")
        vectors = _embed_openai(
            texts, args.base_url, args.api_key, args.model,
            args.dim if args.send_dimensions else 0, args.batch_size,
        )
        dim = len(vectors[0])

    if args.dim and dim != args.dim:
        raise RuntimeError(f"Model returned dim={dim}, expected {args.dim}")

    slug = _slug(args.model)
    build_id = str(manifest.get("build_id") or config.get("collection") or "export")
    out = args.out or (export_dir.parent / f"{build_id}_{slug}")
    out.mkdir(parents=True, exist_ok=True)

    for row, vector in zip(records, vectors):
        row["dense_vector"] = vector
    with gzip.open(out / "chunks.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    for name in ("documents.jsonl", "images.jsonl", "sparse_vocab.json", "golden_retrieval.jsonl"):
        source = export_dir / name
        if source.exists():
            shutil.copy2(source, out / name)
    if (export_dir / "images").is_dir():
        shutil.copytree(export_dir / "images", out / "images", dirs_exist_ok=True)

    collection = f"{config.get('collection', 'bars_kb')}_{slug}"
    old_model = (config.get("embedding") or {}).get("model", "")
    config = {
        **config,
        "collection": collection,
        "embedding": {
            "provider": args.provider,
            "model": args.model,
            "dim": dim,
            "normalize": True,
            "reembedded_from": old_model,
        },
        "vectors": {"dense": {"size": dim, "distance": "Cosine"},
                    "bm25": config.get("vectors", {}).get("bm25", {"modifier": "idf"})},
    }
    _write_json(out / "collection_config.json", config)

    manifest = {
        **manifest,
        "build_id": f"{build_id}_{slug}",
        "collection": collection,
        "reembedded_from": build_id,
        "embedding": config["embedding"],
        "export": {"path": str(out), "chunks": len(records), "images": _count_files(out / "images")},
    }
    _write_json(out / "manifest.json", manifest)
    _write_json(out / "build_info.json", {
        "build_id": manifest["build_id"],
        "collection": collection,
        "reembedded_from": build_id,
        "snapshot": None,
    })
    print(f"written {out}")
    print(f"collection: {collection}")
    print("restore: python examples/restore_from_export.py "
          f"--export {out} --qdrant-url http://127.0.0.1:6333")
    return 0


def _read_chunks(path: Path) -> list[dict]:
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _embed_openai(
    texts: list[str], base_url: str, api_key: str, model: str,
    dimensions: int, batch_size: int,
) -> list[list[float]]:
    result: list[list[float]] = []
    url = base_url.rstrip("/") + "/embeddings"
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        payload: dict = {"model": model, "input": batch, "encoding_format": "float"}
        if dimensions:
            payload["dimensions"] = dimensions
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda item: item.get("index", 0))
        result.extend(item["embedding"] for item in items)
        print(f"  {min(start + batch_size, len(texts))}/{len(texts)}")
    return result


def _embed_local(texts: list[str], model: str, batch_size: int) -> list[list[float]]:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is not installed in this environment. "
            "Install it in the target environment or use --provider openai."
        ) from exc
    encoder = SentenceTransformer(model)
    vectors = encoder.encode(
        texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=True
    )
    return [list(map(float, vector)) for vector in vectors]


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:50] or "model"


def _count_files(path: Path) -> int:
    return sum(1 for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
