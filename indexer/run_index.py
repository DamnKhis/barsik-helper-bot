"""CLI индексации документации. Роль 1.

Запуск локально:  python -m indexer.run_index --docs-dir ./docs
Запуск в docker:  docker compose exec api python -m indexer.run_index --docs-dir /app/docs

Пайплайн: .docx/.md -> блоки -> чанки (путь раздела) -> эмбеддинги -> Qdrant.
Повторный запуск безопасен: точки того же документа перезаписываются.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from indexer.chunking import chunk_document
from indexer.embeddings import EmbeddingClient
from indexer.parse_docx import parse_file
from indexer.qdrant_client import delete_doc, ensure_collection, get_client, upsert_chunks
from settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("indexer")


def index_file(path: Path, client, collection: str, embeddings: EmbeddingClient) -> int:
    doc_name = path.name
    blocks = parse_file(path)
    chunks = chunk_document(doc_name, blocks)
    if not chunks:
        logger.warning("%s: блоки не найдены, пропущен", doc_name)
        return 0
    vectors = embeddings.embed_texts([chunk.text for chunk in chunks])
    delete_doc(client, collection, doc_name)  # идемпотентность повторной индексации
    upserted = upsert_chunks(client, collection, chunks, vectors)
    logger.info("%s: %s блоков -> %s чанков -> %s точек", doc_name, len(blocks), len(chunks), upserted)
    return upserted


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Индексация базы знаний в Qdrant")
    parser.add_argument("--docs-dir", default=settings.docs_dir)
    args = parser.parse_args()

    docs_dir = Path(args.docs_dir)
    files = sorted(p for p in docs_dir.rglob("*") if p.suffix.lower() in {".docx", ".md", ".txt"})
    if not files:
        logger.error("В %s нет .docx/.md файлов — положите исходники документации и повторите", docs_dir)
        return

    client = get_client(settings.qdrant_url)
    ensure_collection(client, settings.qdrant_collection, settings.embedding_dim)
    embeddings = EmbeddingClient(
        settings.embedding_endpoint, settings.embedding_key, settings.embedding_model
    )

    total = 0
    for path in files:
        total += index_file(path, client, settings.qdrant_collection, embeddings)
    logger.info("Готово: всего %s чанков в коллекции %s", total, settings.qdrant_collection)


if __name__ == "__main__":
    main()
