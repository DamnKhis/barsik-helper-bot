from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.handlers import admin as admin_module


def _message(file_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        document=SimpleNamespace(file_name=file_name, file_id="file-id"),
        answer=AsyncMock(),
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(clear=AsyncMock())


def _bot() -> SimpleNamespace:
    async def download(_document, destination: Path) -> None:
        Path(destination).write_bytes(b"document-body")

    return SimpleNamespace(download=AsyncMock(side_effect=download))


def test_upload_saves_to_uploaded_docs_and_kb_source(tmp_path, monkeypatch) -> None:
    uploaded = tmp_path / "uploaded"
    kb_source = tmp_path / "kb-source"
    monkeypatch.setattr(admin_module.settings, "uploaded_docs_dir", str(uploaded))
    monkeypatch.setenv("KB_AUTOSYNC_SOURCE", str(kb_source))
    monkeypatch.delenv("KB_AUTOSYNC", raising=False)
    monkeypatch.setenv("KB_AUTOSYNC_INTERVAL", "30")

    message = _message("reglament.pdf")
    state = _state()
    bot = _bot()

    asyncio.run(admin_module.save_document(message, state, bot))

    assert (uploaded / "reglament.pdf").read_bytes() == b"document-body"
    assert (kb_source / "reglament.pdf").read_bytes() == b"document-body"
    state.clear.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "добавлен в базу знаний" in text
    assert "30 с" in text


def test_upload_rejects_unsupported_format(tmp_path, monkeypatch) -> None:
    uploaded = tmp_path / "uploaded"
    monkeypatch.setattr(admin_module.settings, "uploaded_docs_dir", str(uploaded))
    message = _message("payload.exe")
    state = _state()
    bot = _bot()

    asyncio.run(admin_module.save_document(message, state, bot))

    assert not uploaded.exists()
    bot.download.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert "Поддерживаются PDF" in message.answer.await_args.args[0]


def test_upload_reports_disabled_autosync(tmp_path, monkeypatch) -> None:
    uploaded = tmp_path / "uploaded"
    kb_source = tmp_path / "kb-source"
    monkeypatch.setattr(admin_module.settings, "uploaded_docs_dir", str(uploaded))
    monkeypatch.setenv("KB_AUTOSYNC_SOURCE", str(kb_source))
    monkeypatch.setenv("KB_AUTOSYNC", "0")

    message = _message("manual.docx")
    asyncio.run(admin_module.save_document(message, _state(), _bot()))

    assert (kb_source / "manual.docx").is_file()
    assert "KB_AUTOSYNC=0" in message.answer.await_args.args[0]


def test_upload_reports_current_build_and_starts_confirmation(tmp_path, monkeypatch) -> None:
    """Админу показывают, в какой сборке он оказался, и обещают отчитаться позже."""
    from app.bot.services import kb_status

    uploaded = tmp_path / "uploaded"
    kb_source = tmp_path / "kb-source"
    monkeypatch.setattr(admin_module.settings, "uploaded_docs_dir", str(uploaded))
    monkeypatch.setenv("KB_AUTOSYNC_SOURCE", str(kb_source))
    monkeypatch.delenv("KB_AUTOSYNC", raising=False)

    status = kb_status.KBStatus(build_id="20261114T205237_a5023733", chunks=102, documents=[])
    monkeypatch.setattr(admin_module.kb_status, "snapshot", lambda *a, **k: status)
    started: list[tuple] = []
    monkeypatch.setattr(
        admin_module.asyncio,
        "create_task",
        lambda coro: started.append(coro) or coro.close(),
    )

    message = _message("reglament.pdf")
    message.chat = SimpleNamespace(id=555)

    asyncio.run(admin_module.save_document(message, _state(), _bot()))

    text = message.answer.await_args.args[0]
    assert "Документ добавлен в базу знаний: reglament.pdf" in text
    assert "20261114T205237_a5023733" in text
    assert "документов 0, чанков 102" in text
    assert "Этого документа в сборке пока нет" in text
    assert started, "должно запуститься подтверждение попадания документа в сборку"


def test_upload_skips_confirmation_when_document_already_indexed(tmp_path, monkeypatch) -> None:
    from app.bot.services import kb_status

    uploaded = tmp_path / "uploaded"
    kb_source = tmp_path / "kb-source"
    monkeypatch.setattr(admin_module.settings, "uploaded_docs_dir", str(uploaded))
    monkeypatch.setenv("KB_AUTOSYNC_SOURCE", str(kb_source))
    monkeypatch.delenv("KB_AUTOSYNC", raising=False)

    status = kb_status.KBStatus(
        build_id="b1",
        chunks=5,
        documents=[{"source_path": "docs/source/reglament.pdf", "title": "Регламент"}],
    )
    monkeypatch.setattr(admin_module.kb_status, "snapshot", lambda *a, **k: status)
    tasks: list[tuple] = []
    monkeypatch.setattr(admin_module.asyncio, "create_task", lambda coro: tasks.append(coro) or coro.close())

    message = _message("reglament.pdf")
    message.chat = SimpleNamespace(id=555)

    asyncio.run(admin_module.save_document(message, _state(), _bot()))

    assert "Этот документ в сборке уже есть" in message.answer.await_args.args[0]
    assert tasks == []
