"""Статус базы знаний: какая сборка сейчас отдаётся и всё ли из папки с документами в неё попало.

Зачем: документ можно загрузить через бота, бот радостно ответит «пересборка
запустится в течение 60 с» — а если пересборка не доехала (нет ключа, лежит
Qdrant, watcher не запущен), пользователь просто не получит ответов по новому
документу и никто об этом не узнает. Этот модуль даёт точку правды:

* ``snapshot()`` — какая сборка в ``dist/LATEST``, сколько в ней документов и
  чанков, какой digest исходников в ней зафиксирован;
* ``stale`` — digest папки с документами разошёлся с digest'ом сборки, то есть
  часть документов ещё не пересобрана;
* ``has_document(name)`` — есть ли конкретный файл в текущей сборке.

Digest считается точно так же, как в пайплайне ``bars_kb.pipeline``
(``source_fingerprint``), поэтому сравнение корректно; если bars-kb недоступен,
используется локальная копия той же формулы.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KB_ROOT = PROJECT_ROOT / "bars-kb"
KB_SRC = KB_ROOT / "src"
LATEST_NAME = "LATEST"
SUPPORTED_SUFFIXES = frozenset({".pdf", ".docx", ".md", ".markdown", ".txt"})


def dist_dir() -> Path:
    """Папка пакета знаний (``KB_EXPORT_DIR`` или ``bars-kb/dist``)."""
    raw = os.getenv("KB_EXPORT_DIR", "").strip()
    path = Path(raw).expanduser() if raw else KB_ROOT / "dist"
    return path if path.is_absolute() else PROJECT_ROOT / path


def source_dir() -> Path:
    """Папка с документами, которую пересобирает watcher."""
    raw = os.getenv("KB_AUTOSYNC_SOURCE", "").strip()
    path = Path(raw).expanduser() if raw else KB_ROOT / "docs" / "source"
    if not path.is_absolute():
        path = KB_ROOT / path
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local_fingerprint(files: list[Path], root: Path) -> str:
    """Резервная копия ``bars_kb.pipeline.source_fingerprint`` (та же формула)."""
    digest = hashlib.sha256()
    for path in sorted(files):
        try:
            key = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            key = path.name
        digest.update(f"{key}\0{_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def source_digest(source: Path | None = None) -> str:
    """Digest документов, лежащих в папке-источнике (как в манифесте сборки)."""
    source = source or source_dir()
    if not source.is_dir():
        return ""
    files = sorted(p for p in source.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        return ""
    if str(KB_SRC) not in sys.path:
        sys.path.insert(0, str(KB_SRC))
    try:
        from bars_kb.pipeline import source_fingerprint  # noqa: PLC0415

        return source_fingerprint(files, source)[0]
    except Exception as exc:  # bars-kb недоступен — считаем сами
        logger.debug("source_fingerprint из bars-kb недоступен (%s), считаю локально", exc)
        return _local_fingerprint(files, source)


@dataclass(slots=True)
class KBStatus:
    """Снимок текущей сборки."""

    build_id: str
    collection: str = ""
    created_at: str = ""
    chunks: int = 0
    documents: list[dict] = field(default_factory=list)
    build_digest: str = ""
    current_digest: str = ""

    @property
    def stale(self) -> bool:
        """Документы в папке изменились после последней сборки."""
        return bool(self.current_digest) and self.build_digest != self.current_digest

    @property
    def document_names(self) -> set[str]:
        return {Path(str(doc.get("source_path", ""))).name.lower() for doc in self.documents}

    def has_document(self, name: str | Path) -> bool:
        return Path(str(name)).name.lower() in self.document_names

    def title_of(self, name: str | Path) -> str:
        target = Path(str(name)).name.lower()
        for doc in self.documents:
            if Path(str(doc.get("source_path", ""))).name.lower() == target:
                return str(doc.get("title") or "")
        return ""

    def short(self) -> str:
        return (
            f"сборка {self.build_id}: документов {len(self.documents)}, "
            f"чанков {self.chunks}"
        )

    def document_list(self, limit: int = 8) -> str:
        titles = [str(doc.get("title") or doc.get("doc_id") or "?") for doc in self.documents[:limit]]
        tail = "…" if len(self.documents) > limit else ""
        return ", ".join(titles) + tail

    def as_dict(self) -> dict:
        return {
            "build_id": self.build_id,
            "collection": self.collection,
            "created_at": self.created_at,
            "chunks": self.chunks,
            "documents": len(self.documents),
            "stale": self.stale,
        }


def current_build_dir(base: Path | None = None) -> Path | None:
    """Папка сборки, которую отдаёт ``dist/LATEST`` (или ``None``)."""
    base = base or dist_dir()
    latest = base / LATEST_NAME
    if not latest.is_file():
        return None
    build_id = latest.read_text(encoding="utf-8").strip()
    if not build_id:
        return None
    candidate = base / build_id
    return candidate if (candidate / "manifest.json").is_file() else None


def snapshot(source: Path | None = None, base: Path | None = None) -> KBStatus | None:
    """Собрать статус текущей сборки; ``None`` — пакета ещё нет."""
    build_dir = current_build_dir(base)
    if build_dir is None:
        return None
    try:
        manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Не читается манифест сборки %s: %s", build_dir, exc)
        return None

    documents = [
        {
            "doc_id": doc.get("doc_id", ""),
            "title": doc.get("title", ""),
            "source_path": doc.get("source_path", ""),
            "chunk_count": int(doc.get("chunk_count", 0) or 0),
        }
        for doc in (manifest.get("documents") or [])
        if isinstance(doc, dict)
    ]
    return KBStatus(
        build_id=str(manifest.get("build_id") or build_dir.name),
        collection=str(manifest.get("collection") or ""),
        created_at=str(manifest.get("created_at") or ""),
        chunks=int((manifest.get("counts") or {}).get("chunks", 0) or 0),
        documents=documents,
        build_digest=str((manifest.get("source") or {}).get("digest", "")),
        current_digest=source_digest(source),
    )
