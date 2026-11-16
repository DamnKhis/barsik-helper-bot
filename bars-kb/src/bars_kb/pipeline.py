from __future__ import annotations

import gzip
import hashlib
import json
import logging
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .checksums import write_checksums
from .chunker import Chunker
from .config import Settings
from .embeddings import build_embedder
from .embeddings.sparse import build_vocab, encode_sparse
from .exports import prune_builds, write_latest
from .models import Chunk, Document
from .parsers import SUPPORTED, parse_document
from .providers import build_llm, build_vision
from .store.qdrant_store import QdrantStore, vector_to_json

PIPELINE_VERSION = "0.1.0"

logger = logging.getLogger(__name__)


def iter_sources(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.suffix.lower() in SUPPORTED)


@dataclass(frozen=True)
class SourceFile:
    """Файл-источник: сам путь, его корень и вид (doc/support)."""

    path: Path
    root: Path
    kind: str = "doc"

    @property
    def key(self) -> str:
        """Ключ для digest: ``doc/source/...`` или ``support/support_answers/...``."""
        try:
            relative = self.path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            relative = self.path.name
        return f"{self.kind}/{self.root.name}/{relative}"


def collect_sources(
    settings: Settings,
    sources: list[tuple[Path, str]] | None = None,
) -> list[SourceFile]:
    """Собрать файлы всех источников: документация + решённые обращения.

    Отсутствующая папка не ошибка: набор тикетов может быть ещё не создан.
    """
    collected: list[SourceFile] = []
    if isinstance(sources, (str, Path)):  # одиночная папка — как было раньше
        sources = [(Path(sources), "doc")]
    for root, kind in sources if sources is not None else settings.source_roots():
        root = Path(root)
        if not root.exists():
            logger.debug("source folder is missing, skipping: %s (%s)", root, kind)
            continue
        for path in iter_sources(root):
            collected.append(SourceFile(path=path, root=root if root.is_dir() else root.parent, kind=kind))
    return collected


def fingerprint_sources(files: list[SourceFile]) -> tuple[str, list[dict]]:
    """Digest по всем источникам сразу (kind + путь + содержимое)."""
    records: list[dict] = []
    for entry in files:
        records.append(
            {
                "path": entry.key,
                "kind": entry.kind,
                "sha256": file_sha256(entry.path),
                "size": entry.path.stat().st_size,
            }
        )
    records.sort(key=lambda item: item["path"])
    digest = hashlib.sha256()
    for record in records:
        digest.update(f"{record['path']}\0{record['sha256']}\n".encode("utf-8"))
    return digest.hexdigest(), records


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(files: list[Path], root: Path | None = None) -> tuple[str, list[dict]]:
    """Hash document paths and contents; independent of the absolute source root."""
    records: list[dict] = []
    for path in sorted(files):
        if root is not None:
            try:
                key = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                key = path.name
        else:
            key = path.name
        records.append(
            {
                "path": key,
                "sha256": file_sha256(path),
                "size": path.stat().st_size,
            }
        )

    digest = hashlib.sha256()
    for record in records:
        digest.update(f"{record['path']}\0{record['sha256']}\n".encode("utf-8"))
    return digest.hexdigest(), records


def _group_sources(records: list[dict]) -> list[dict]:
    """Сводка по источникам для манифеста: сколько файлов в каждой папке."""
    grouped: dict[str, dict] = {}
    for record in records:
        kind = str(record.get("kind") or "doc")
        bucket = grouped.setdefault(kind, {"kind": kind, "digest": "", "files": []})
        bucket["files"].append(record["path"])
    for bucket in grouped.values():
        digest = hashlib.sha256()
        for record in records:
            if str(record.get("kind") or "doc") == bucket["kind"]:
                digest.update(f"{record['path']}\0{record['sha256']}\n".encode("utf-8"))
        bucket["digest"] = digest.hexdigest()
    return [grouped[key] for key in sorted(grouped)]


def build_id_for(paths: list[Path], root: Path | None = None) -> str:
    digest, _ = source_fingerprint(paths, root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{digest[:8]}"


def ingest(
    settings: Settings,
    source: Path | None = None,
    export: bool = True,
    keep: int = 2,
    evaluate: bool = False,
    eval_top_k: int = 8,
    sources: list[tuple[Path, str]] | None = None,
) -> dict:
    """Собрать сборку из всех источников.

    ``source`` — совместимость: одна папка документации. ``sources`` — список
    ``(папка, вид)``, где вид ``doc`` или ``support``; по умолчанию берутся
    ``settings.source_roots()`` (документация и решённые обращения).
    """
    if sources is None:
        sources = [(source, "doc")] if source is not None else settings.source_roots()
    files = collect_sources(settings, sources)
    if not files:
        raise RuntimeError(
            "No supported documents found in " + ", ".join(str(root) for root, _ in sources)
        )

    source_digest, source_records = fingerprint_sources(files)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    build_id = f"{stamp}_{source_digest[:8]}"
    collection = f"{settings.collection_prefix}_{build_id}"
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "builds").mkdir(parents=True, exist_ok=True)

    llm = build_llm(settings)
    vision = build_vision(settings)
    chunker = Chunker(settings, llm, vision)
    embedder = build_embedder(settings)

    documents: list[Document] = []
    all_chunks: list[Chunk] = []
    document_records: list[dict] = []

    for index, entry in enumerate(files, start=1):
        path = entry.path
        sha = file_sha256(path)
        logger.info("[%d/%d] parse %s (%s)", index, len(files), path.name, entry.kind)
        document = parse_document(
            path,
            settings.images_dir,
            version=sha[:16],
            kind=entry.kind,
            support_mode=settings.support_index_mode,
        )
        portable_path = _portable_path(settings, path)
        document.source_path = portable_path
        document.metadata["sha256"] = sha
        document.metadata["source_path"] = portable_path
        if settings.github_repo:
            github_root = settings.data_dir / "github" / settings.github_branch
            try:
                relative = path.resolve().relative_to(github_root.resolve()).as_posix()
            except ValueError:
                relative = f"{settings.github_docs_path}/{path.name}"
            document.metadata["github_url"] = (
                f"https://github.com/{settings.github_repo}/blob/{settings.github_branch}/{relative}"
            )
        chunks = chunker.document_chunks(document)
        if not chunks:
            logger.warning(
                "no chunks for %s (%s) — документ пропущен (для тикета это значит, что ответа поддержки в нём нет)",
                path.name,
                entry.kind,
            )
            continue
        for chunk in chunks:
            chunk.meta["build_id"] = build_id
        logger.info(
            "    sections=%d chunks=%d",
            len(document.sections),
            len(chunks),
        )
        documents.append(document)
        all_chunks.extend(chunks)
        document_records.append(
            {
                "doc_id": document.doc_id,
                "title": document.title,
                "source_path": document.source_path,
                "source_type": document.source_type,
                "version": document.version,
                "sha256": sha,
                "page_count": document.page_count,
                "section_count": len(document.sections),
                "chunk_count": len(chunks),
                "kind": entry.kind,
                "github_url": document.metadata.get("github_url", ""),
            }
        )

    logger.info("embedding %d chunks with %s", len(all_chunks), settings.embedding_provider)
    texts = [chunk.enriched_text for chunk in all_chunks]
    dense_vectors = embedder.embed_documents(texts)
    vocab = build_vocab(texts)
    sparse_vectors = [encode_sparse(text, vocab) for text in texts]

    logger.info("writing Qdrant collection %s", collection)
    store = QdrantStore(settings, collection)
    try:
        store.recreate()
        store.upsert(all_chunks, dense_vectors, sparse_vectors)
        store.set_alias(f"{settings.collection_prefix}_current", keep_previous=True)
        count = store.count()
        snapshot = store.create_snapshot()
    finally:
        store.close()
    write_json(settings.data_dir / "sparse_vocab.json", vocab)
    exported = (
        export_build(
            settings,
            build_id,
            collection,
            documents,
            all_chunks,
            document_records,
            snapshot,
            source_digest,
        )
        if export
        else {}
    )

    manifest = {
        "build_id": build_id,
        "collection": collection,
        "alias": f"{settings.collection_prefix}_current",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": PIPELINE_VERSION,
        "source": {"digest": source_digest, "files": source_records},
        "sources": _group_sources(source_records),
        "embedding": {
            "provider": settings.embedding_provider if not settings.mock_embedding else "hash",
            "model": settings.qwen_embedding_model if not settings.mock_embedding else "hash-lexical",
            "dim": settings.embedding_dim,
        },
        "enrichment": {
            "llm_provider": "mock" if settings.mock_llm else "qwen",
            "llm_model": "mock" if settings.mock_llm else settings.qwen_llm_model,
            "vision_provider": "mock" if settings.mock_vision else "qwen",
            "vision_model": "mock" if settings.mock_vision else settings.qwen_vision_model,
        },
        "sparse": {"type": "bm25", "vocab_size": len(vocab), "modifier": "idf"},
        "documents": document_records,
        "counts": {
            "documents": len(documents),
            "chunks": len(all_chunks),
            "support_documents": sum(1 for record in document_records if record.get("kind") == "support"),
            "images": sum(1 for c in all_chunks if c.image_path),
            "faq": sum(1 for c in all_chunks if c.chunk_type == "faq"),
            "tables": sum(1 for c in all_chunks if c.chunk_type == "table"),
            "diagrams": sum(1 for c in all_chunks if c.chunk_type == "diagram"),
            "screenshots": sum(1 for c in all_chunks if c.chunk_type == "screenshot"),
        },
        "qdrant": {"count": count, "snapshot": snapshot, "mode": "server" if settings.qdrant_url else "embedded"},
        "export": exported,
    }
    if export:
        write_latest(settings.dist_dir, build_id)
        pruned = prune_builds(settings.dist_dir, keep=keep, current=build_id)
        manifest["export"] = {**exported, "latest": build_id, "pruned": pruned}
    write_json(settings.data_dir / "manifest.json", manifest)
    write_json(settings.data_dir / "builds" / f"{build_id}.json", manifest)
    if export:
        write_json(settings.dist_dir / build_id / "manifest.json", manifest)
        write_checksums(settings.dist_dir / build_id)
    if evaluate and export:
        try:
            from .eval.runner import run_eval

            report = run_eval(settings, top_k=eval_top_k)
            logger.info(
                "eval: hit@1=%.4f hit@3=%.4f hit@5=%.4f mrr=%.4f",
                report["hit@1"],
                report["hit@3"],
                report["hit@5"],
                report["mrr"],
            )
        except Exception as exc:
            logger.warning("eval failed; publishing without eval_report: %s", exc)
    logger.info("counts: %s", json.dumps(manifest["counts"], ensure_ascii=False))
    return manifest


def export_build(
    settings: Settings,
    build_id: str,
    collection: str,
    documents: list[Document],
    chunks: list[Chunk],
    document_records: list[dict],
    snapshot: str | None,
    source_digest: str = "",
) -> dict:
    out = settings.dist_dir / build_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(exist_ok=True)

    store = QdrantStore(settings, collection)
    try:
        points = store.scroll_all()
    finally:
        store.close()

    with gzip.open(out / "chunks.jsonl.gz", "wt", encoding="utf-8") as handle:
        for point in points:
            payload = point.payload or {}
            record = {
                "id": str(point.id),
                "dense_vector": _dense(point.vector),
                "sparse_vector": _sparse(point.vector),
                "payload": payload,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    with (out / "documents.jsonl").open("w", encoding="utf-8") as handle:
        for record in document_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    image_records: list[dict] = []
    for chunk in chunks:
        if not chunk.image_path:
            continue
        source = settings.data_dir / chunk.image_path
        target = out / chunk.image_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not target.exists():
            shutil.copy2(source, target)
        image_records.append(
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "page": chunk.page,
                "path": chunk.image_path,
                "sha256": chunk.meta.get("image_sha256", ""),
            }
        )
    with (out / "images.jsonl").open("w", encoding="utf-8") as handle:
        for record in image_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    collection_config = {
        "collection": collection,
        "alias": f"{settings.collection_prefix}_current",
        "vectors": {
            "dense": {"size": settings.embedding_dim, "distance": "Cosine"},
            "bm25": {"modifier": "idf"},
        },
        "embedding": {
            "provider": settings.embedding_provider if not settings.mock_embedding else "hash",
            "model": settings.qwen_embedding_model if not settings.mock_embedding else "hash-lexical",
            "dim": settings.embedding_dim,
            "normalize": True,
        },
        "sparse": {"type": "bm25", "language": "ru"},
        "payload_indexes": ["doc_id", "chunk_type", "version", "tags", "doc_kind"],
        "snapshot": snapshot,
    }
    write_json(out / "collection_config.json", collection_config)
    write_json(out / "manifest.json", {"build_id": build_id, "collection": collection, "documents": document_records})

    for name in ("golden_retrieval.jsonl", "sparse_vocab.json"):
        source = settings.data_dir / name
        if source.exists():
            shutil.copy2(source, out / name)

    write_json(out / "build_info.json", {
        "build_id": build_id,
        "collection": collection,
        "snapshot": snapshot,
        "source_digest": source_digest,
    })
    _write_package_readme(
        out, settings, build_id, collection, chunks, document_records, snapshot, source_digest
    )
    try:
        portable_out = out.relative_to(settings.root).as_posix()
    except ValueError:
        portable_out = str(out)
    return {"path": portable_out, "chunks": len(points), "images": len(image_records)}


def _dense(vector) -> list[float]:
    if isinstance(vector, dict):
        value = vector.get("dense", [])
        return list(value)
    if isinstance(vector, list):
        return list(vector)
    return []


def _sparse(vector) -> dict:
    if isinstance(vector, dict):
        value = vector.get("bm25")
        if value is not None:
            return vector_to_json(value)
    return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _portable_path(settings: Settings, path: Path) -> str:
    try:
        return path.resolve().relative_to(settings.root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_package_readme(
    out: Path,
    settings: Settings,
    build_id: str,
    collection: str,
    chunks: list[Chunk],
    document_records: list[dict],
    snapshot: str | None,
    source_digest: str = "",
) -> None:
    counts = Counter(chunk.chunk_type for chunk in chunks)
    embedding_model = (
        settings.qwen_embedding_model if not settings.mock_embedding else "hash-lexical"
    )
    embedding_provider = settings.embedding_provider if not settings.mock_embedding else "hash"
    enrichment_model = (
        f"`{settings.qwen_llm_model}`" if not settings.mock_llm else "`mock`"
    )
    vision_model = (
        f"`{settings.qwen_vision_model}`" if not settings.mock_vision else "`mock`"
    )
    text = f"""# BARS KB — пакет передачи

Собран пайплайном `bars-kb` версии `{PIPELINE_VERSION}`.

| Параметр | Значение |
|---|---|
| build_id | `{build_id}` |
| коллекция | `{collection}` |
| alias | `{settings.collection_prefix}_current` |
| предыдущая версия | `{settings.collection_prefix}_prev` |
| source digest | `{source_digest[:16] or "—"}` |
| создан | {datetime.now(timezone.utc).isoformat()} |
| эмбеддинги | `{embedding_provider}` / `{embedding_model}`, dim `{settings.embedding_dim}` |
| текст и JSON | {enrichment_model} |
| скриншоты и схемы | {vision_model} |
| sparse | BM25, IDF |
| snapshot | `{snapshot or "нет (embedded Qdrant)"}` |

## Состав

| Файл | Что внутри |
|---|---|
| `chunks.jsonl.gz` | по одному JSON на чанк: `id`, `dense_vector`, `sparse_vector`, `payload` |
| `documents.jsonl` | документы, версии, sha256, число разделов и чанков |
| `images.jsonl` | связь `chunk_id` → файл изображения |
| `images/` | {counts.get("screenshot", 0)} скриншотов и {counts.get("diagram", 0)} схема |
| `collection_config.json` | имя коллекции, alias, named vectors, модель эмбеддингов |
| `manifest.json` | полный манифест сборки, counts, документы, Qdrant |
| `build_info.json` | краткая информация о сборке |
| `sparse_vocab.json` | словарь BM25; обязателен для sparse-поиска |
| `golden_retrieval.jsonl` | контрольные вопросы для eval |
| `eval_report.json` | метрики hit@1/3/5, MRR и примеры выдачи (после `bars-kb eval`) |
| `checksums.sha256` | SHA256 всех файлов пакета |

## Данные

- документов: **{len(document_records)}** (из них решённых обращений поддержки:
  **{sum(1 for record in document_records if record.get("kind") == "support")}**)
- чанков: **{len(chunks)}**
- text: {counts.get("text", 0)}, faq: {counts.get("faq", 0)}, table: {counts.get("table", 0)},
  screenshot: {counts.get("screenshot", 0)}, diagram: {counts.get("diagram", 0)}

Подробная схема payload — в `HANDOFF.md` проекта. Кратко: поиск идёт по `enriched_text`,
ответ и цитата — по `raw_text`; `parent_text` даёт контекст раздела; `section_path` и
`page` — для ссылки на источник. Поле `doc_kind` отличает документацию (`doc`) от
решённых обращений поддержки (`support`), `source_path` ведёт к исходному файлу.

## Восстановление

Из корня проекта (выбирается сборка из `dist/LATEST`):

```bash
# Qdrant-сервер
python examples/restore_from_export.py \\
  --export dist \\
  --qdrant-url http://127.0.0.1:6333 \\
  --alias {settings.collection_prefix}_current

# локальный embedded Qdrant
python examples/restore_from_export.py \\
  --export dist --qdrant-path data/qdrant-restored
```

## Проверка целостности

```bash
cd {out.name} && sha256sum -c checksums.sha256
```

## Поиск

```bash
python examples/query.py "Почему не работает фильтр !> 200?" --top-k 5
```

## Модель эмбеддингов

Вектор запроса обязан считаться той же моделью: **`{embedding_model}`**, dim **{settings.embedding_dim}**.
Если модель другая — `examples/reembed_from_export.py` пересчитает только dense-векторы.
"""
    (out / "README_HANDOFF.md").write_text(text, encoding="utf-8")
