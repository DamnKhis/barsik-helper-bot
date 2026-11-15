"""Тесты статуса базы знаний (без сети и без Qdrant)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.services import kb_status


def _make_build(base: Path, build_id: str, source_path: str, *, digest: str = "abc123") -> None:
    build = base / build_id
    build.mkdir(parents=True, exist_ok=True)
    (build / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "collection": f"bars_kb_{build_id}",
                "created_at": "2026-11-16T00:00:00+00:00",
                "counts": {"documents": 1, "chunks": 18},
                "documents": [
                    {
                        "doc_id": "bp-propusk",
                        "title": "БП Пропуск",
                        "source_path": source_path,
                        "chunk_count": 18,
                    }
                ],
                "source": {"digest": digest},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "LATEST").write_text(build_id + "\n", encoding="utf-8")


def test_snapshot_reads_latest_build(tmp_path) -> None:
    _make_build(tmp_path, "20261116T000000_deadbeef", "docs/source/БП Пропуск.docx")
    source = tmp_path / "docs" / "source"
    source.mkdir(parents=True)
    (source / "БП Пропуск.docx").write_bytes(b"docx")

    status = kb_status.snapshot(source=source, base=tmp_path)

    assert status is not None
    assert status.build_id == "20261116T000000_deadbeef"
    assert status.collection.startswith("bars_kb_")
    assert status.chunks == 18
    assert status.has_document("БП Пропуск.docx")
    assert status.has_document(Path("/tmp/uploaded/БП Пропуск.docx"))
    assert not status.has_document("Реестр мебели.pdf")
    assert status.title_of("БП Пропуск.docx") == "БП Пропуск"
    assert "документов 1" in status.short()
    assert status.as_dict()["documents"] == 1


def test_snapshot_is_none_without_latest(tmp_path) -> None:
    assert kb_status.snapshot(base=tmp_path) is None


def test_stale_when_sources_changed(tmp_path) -> None:
    _make_build(tmp_path, "b1", "docs/source/БП Пропуск.docx", digest="old-digest")
    source = tmp_path / "docs" / "source"
    source.mkdir(parents=True)
    (source / "БП Пропуск.docx").write_bytes(b"docx")
    (source / "Новый регламент.pdf").write_bytes(b"%PDF-1.4")

    status = kb_status.snapshot(source=source, base=tmp_path)

    assert status is not None and status.stale is True


def test_digest_matches_bars_kb_pipeline(tmp_path) -> None:
    """Наш digest обязан совпадать с тем, что пишет пайплайн в manifest.source.digest."""
    bars_kb_pipeline = pytest.importorskip("bars_kb.pipeline")
    source = tmp_path / "docs"
    source.mkdir()
    (source / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (source / "b.docx").write_bytes(b"docx b")
    (source / "ignore.bin").write_bytes(b"bin")

    files = [p for p in sorted(source.iterdir()) if p.suffix.lower() in kb_status.SUPPORTED_SUFFIXES]
    expected = bars_kb_pipeline.source_fingerprint(files, source)[0]
    assert kb_status.source_digest(source) == expected


def test_source_dir_and_dist_dir_follow_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KB_AUTOSYNC_SOURCE", str(tmp_path / "docs"))
    monkeypatch.setenv("KB_EXPORT_DIR", str(tmp_path / "dist"))
    assert kb_status.source_dir() == tmp_path / "docs"
    assert kb_status.dist_dir() == tmp_path / "dist"


def test_confirm_indexed_reports_success(monkeypatch, tmp_path) -> None:
    """Как только документ появился в сборке — админу уходит ✅."""
    from app.bot.handlers import admin as admin_module

    monkeypatch.setenv("KB_AUTOSYNC_INTERVAL", "5")
    monkeypatch.setenv("KB_CONFIRM_TIMEOUT_SEC", "30")
    status = kb_status.KBStatus(build_id="b2", chunks=120, documents=[{"source_path": "docs/source/новый.docx"}])
    monkeypatch.setattr(admin_module.kb_status, "snapshot", lambda *a, **k: status)

    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(admin_module._confirm_indexed(bot, chat_id=7, name="новый.docx"))

    text = bot.send_message.await_args.args[1]
    assert text.startswith("✅")
    assert "b2" in text


def test_confirm_indexed_warns_when_stale(monkeypatch) -> None:
    """Если пересборка не доехала — админ получает предупреждение с подсказкой."""
    from app.bot.handlers import admin as admin_module

    monkeypatch.setenv("KB_AUTOSYNC_INTERVAL", "5")
    monkeypatch.setenv("KB_CONFIRM_TIMEOUT_SEC", "5")
    stale = kb_status.KBStatus(build_id="b1", chunks=102, build_digest="old", current_digest="new")
    monkeypatch.setattr(admin_module.kb_status, "snapshot", lambda *a, **k: stale)
    monkeypatch.setattr(admin_module.asyncio, "sleep", AsyncMock())

    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(admin_module._confirm_indexed(bot, chat_id=7, name="БП Пропуск.docx"))

    text = bot.send_message.await_args.args[1]
    assert text.startswith("⚠️")
    assert "пересборка не прошла" in text.lower()
    assert "bars-kb sync" in text


def test_confirm_indexed_warns_without_package(monkeypatch) -> None:
    from app.bot.handlers import admin as admin_module

    monkeypatch.setenv("KB_AUTOSYNC_INTERVAL", "5")
    monkeypatch.setenv("KB_CONFIRM_TIMEOUT_SEC", "5")
    monkeypatch.setattr(admin_module.kb_status, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(admin_module.asyncio, "sleep", AsyncMock())

    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(admin_module._confirm_indexed(bot, chat_id=7, name="БП Пропуск.docx"))

    assert "Пакета знаний ещё нет" in bot.send_message.await_args.args[1]
