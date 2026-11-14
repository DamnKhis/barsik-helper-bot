"""Реиндексация базы знаний при изменении Git-репозитория. Роль 4.

Два механизма («Будущие возможности» п.2 брифа):
- поллинг: apscheduler раз в N секунд сравнивает SHA последнего коммита и при
  изменении переиндексирует файлы документации. Работает без публичного URL —
  важно на хакатоне (ноутбук за NAT, ngrok не нужен);
- вебхуки: POST /webhook/github и /webhook/gitlab в app/main.py вызывают
  reindex_files() сразу после пуша (точечно, только изменённые файлы).
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from qdrant_client import QdrantClient

from app.analytics.db import log_reindex
from app.integrations.git_provider import GitProvider, make_provider
from indexer.chunking import chunk_document
from indexer.embeddings import EmbeddingClient
from indexer.blocks import parse_file, parse_markdown
from indexer.qdrant_client import delete_doc, ensure_collection, upsert_chunks
from settings import Settings

logger = logging.getLogger(__name__)


def index_content(
    doc_name: str, content: bytes, client: QdrantClient, collection: str, embeddings: EmbeddingClient
) -> int:
    """Один документ: bytes -> блоки -> чанки -> Qdrant. Возвращает число чанков."""
    suffix = Path(doc_name).suffix.lower()
    if suffix in {".md", ".txt"}:
        blocks = parse_markdown(content.decode("utf-8"))
    else:  # .pdf / .docx — парсеру нужен файл на диске
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            blocks = parse_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    chunks = chunk_document(doc_name, blocks)
    if not chunks:
        return 0
    vectors = embeddings.embed_texts([chunk.text for chunk in chunks])
    delete_doc(client, collection, doc_name)
    return upsert_chunks(client, collection, chunks, vectors)


def _reindex(provider: GitProvider, paths: list[str], settings: Settings, source: str) -> dict:
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    ensure_collection(client, settings.qdrant_collection, settings.embedding_dim)
    embeddings = EmbeddingClient(
        settings.embedding_endpoint, settings.embedding_key, settings.embedding_model
    )
    indexed, errors = 0, 0
    for path in paths:
        try:
            content = provider.download_file(path)
            done = index_content(Path(path).name, content, client, settings.qdrant_collection, embeddings)
            indexed += 1 if done else 0
        except Exception as exc:  # noqa: BLE001 — один битый файл не рушит остальное
            errors += 1
            logger.error("Реиндексация %s не удалась: %s", path, exc)
    status = "ok" if errors == 0 else "partial"
    log_reindex(settings.db_path, source, len(paths), status, f"indexed={indexed} errors={errors}")
    return {"files": len(paths), "indexed": indexed, "errors": errors, "status": status}


def reindex_files(settings: Settings, paths: list[str], source: str) -> dict:
    """Точечная реиндексация изменённых файлов (вебхук)."""
    if not settings.docs_repo_url:
        logger.warning("DOCS_REPO_URL не задан — вебхук пропущен")
        return {"status": "skipped", "reason": "DOCS_REPO_URL не задан"}
    provider = make_provider(settings.docs_repo_url, settings.docs_repo_branch, settings.docs_repo_token)
    return _reindex(provider, paths, settings, source)


def reindex_all(settings: Settings, source: str) -> dict:
    """Полная переиндексация документации из репозитория (поллинг)."""
    if not settings.docs_repo_url:
        return {"status": "skipped", "reason": "DOCS_REPO_URL не задан"}
    provider = make_provider(settings.docs_repo_url, settings.docs_repo_branch, settings.docs_repo_token)
    paths = provider.list_doc_files()
    return _reindex(provider, paths, settings, source)


def start_poller(settings: Settings) -> AsyncIOScheduler | None:
    """Поллинг последнего коммита; не требует публичного URL."""
    if not settings.docs_repo_url:
        logger.info("DOCS_REPO_URL не задан — Git-поллинг выключен")
        return None

    provider = make_provider(settings.docs_repo_url, settings.docs_repo_branch, settings.docs_repo_token)
    state = {"sha": None}
    scheduler = AsyncIOScheduler()

    async def poll() -> None:
        try:
            sha = await asyncio.to_thread(provider.latest_commit_sha)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Поллинг Git не удался: %s", exc)
            return
        if not sha or sha == state["sha"]:
            return
        first_check = state["sha"] is None
        state["sha"] = sha
        if first_check:
            logger.info("Git-поллинг: стартовое состояние %s", sha[:8])
            return  # на старте не переиндексируем — база уже собрана run_index
        logger.info("Новый коммит %s — полная реиндексация документации", sha[:8])
        result = await asyncio.to_thread(reindex_all, settings, "polling")
        logger.info("Реиндексация: %s", result)

    scheduler.add_job(poll, "interval", seconds=settings.docs_poll_interval_sec)
    scheduler.start()
    logger.info("Git-поллинг запущен: %s (%s сек)", settings.docs_repo_url, settings.docs_poll_interval_sec)
    return scheduler
