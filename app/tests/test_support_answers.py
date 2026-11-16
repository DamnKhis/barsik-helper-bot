from __future__ import annotations

import asyncio

from app.bot.services.jira import (
    BOT_TRANSCRIPT_PREFIX,
    BOT_USER_PREFIX,
    JiraAttachment,
    JiraComment,
)
from app.bot.services.support_answers import (
    is_useful_status,
    render_support_answer,
    safe_filename,
    save_support_answer,
)


class FakeClient:
    def __init__(self, attachments=None, content: bytes = b"", fail_download: bool = False) -> None:
        self.attachments = attachments or []
        self.content = content
        self.fail_download = fail_download

    async def list_attachments(self, issue_key: str) -> list[JiraAttachment]:
        return self.attachments

    async def download(self, url: str) -> bytes:
        if self.fail_download:
            raise RuntimeError("download failed")
        return self.content


def _comment(comment_id: int, body: str, author: str = "Поддержка") -> JiraComment:
    return JiraComment(id=comment_id, author=author, author_email="support@example.com", body=body)


def _dialog() -> list[JiraComment]:
    return [
        _comment(1, f"{BOT_TRANSCRIPT_PREFIX}\nОбращение пользователя..."),
        _comment(2, f"{BOT_USER_PREFIX}42]\nИван (@ivan), Telegram ID 42:\nКак оформить пропуск?"),
        _comment(3, "Здравствуйте! Нужен паспорт и заявка от руководителя."),
        _comment(4, f"{BOT_USER_PREFIX}42]\nИван (@ivan), Telegram ID 42:\nА сколько ждать?"),
        _comment(5, "Обычно один рабочий день."),
    ]


def test_is_useful_status() -> None:
    assert is_useful_status("Полезно")
    assert is_useful_status("полезный ответ")
    assert is_useful_status("Useful")
    assert not is_useful_status("Готово")
    assert not is_useful_status("В работе")


def test_render_support_answer_builds_qa_pairs() -> None:
    content = render_support_answer(
        "KAN-12",
        "https://jira/browse/KAN-12",
        "Как оформить пропуск",
        _dialog(),
    )
    assert content is not None
    assert "# KAN-12: полезный ответ поддержки" in content
    assert "- Тема: Как оформить пропуск" in content
    assert "## Вопрос 1" in content
    assert "Как оформить пропуск?" in content
    assert "## Ответ поддержки 1" in content
    assert "Нужен паспорт и заявка от руководителя." in content
    assert "## Вопрос 2" in content
    assert "А сколько ждать?" in content
    assert "Обычно один рабочий день." in content
    assert BOT_TRANSCRIPT_PREFIX not in content
    assert "[BARS-USER" not in content


def test_render_support_answer_dialog_without_support_reply() -> None:
    comments = [_comment(2, f"{BOT_USER_PREFIX}42]\nИван:\nВопрос без ответа")]
    content = render_support_answer("KAN-1", "", "", comments)
    assert content is not None
    assert "## Диалог" in content
    assert "**Пользователь:** Вопрос без ответа" in content


def test_render_support_answer_none_without_any_content() -> None:
    assert render_support_answer("KAN-1", "", "", []) is None


def test_save_support_answer_writes_and_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.bot.services.support_answers.settings.support_answers_dir",
        str(tmp_path / "support_answers"),
    )
    client = FakeClient()
    first = asyncio.run(
        save_support_answer(client, "KAN-12", "https://jira/browse/KAN-12", "Тема", _dialog())
    )
    assert first is not None and first.is_file()
    content = first.read_text(encoding="utf-8")
    assert "Нужен паспорт" in content

    second = asyncio.run(
        save_support_answer(client, "KAN-12", "https://jira/browse/KAN-12", "Тема", _dialog())
    )
    assert second == first
    assert first.read_text(encoding="utf-8") == content
    assert list(first.parent.glob("*.tmp")) == []


def test_save_support_answer_downloads_attachment_and_embeds_image(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.bot.services.support_answers.settings.support_answers_dir",
        str(tmp_path / "support_answers"),
    )
    comments = [
        _comment(
            2,
            f"{BOT_USER_PREFIX}42]\nИван (@ivan):\nСмотрите скрин (вложение: photo_7.jpg)",
        ),
        _comment(3, "Вижу проблему, сейчас поправим."),
    ]
    client = FakeClient(
        attachments=[
            JiraAttachment(
                id="9",
                filename="photo_7.jpg",
                mime_type="image/jpeg",
                size=9,
                content_url="https://jira.example.com/file/9",
            )
        ],
        content=b"image-bytes",
    )

    path = asyncio.run(
        save_support_answer(client, "KAN-13", "https://jira/browse/KAN-13", "Скрин", comments)
    )

    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "![photo_7.jpg](attachments/KAN-13/photo_7.jpg)" in content
    stored = tmp_path / "support_answers" / "attachments" / "KAN-13" / "photo_7.jpg"
    assert stored.read_bytes() == b"image-bytes"


def test_safe_filename_removes_unsafe_chars() -> None:
    assert safe_filename("KAN-12") == "KAN-12.md"
    assert safe_filename("../../etc/passwd") == "passwd.md"


def test_save_support_answer_skips_empty_ticket(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.bot.services.support_answers.settings.support_answers_dir",
        str(tmp_path / "support_answers"),
    )

    class TrackingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def list_attachments(self, issue_key: str):
            self.calls += 1
            return []

    client = TrackingClient()

    result = asyncio.run(save_support_answer(client, "KAN-1", "", "", []))

    assert result is None
    assert client.calls == 0
    assert not (tmp_path / "support_answers").exists()


def test_render_support_answer_links_bang_attachment() -> None:
    comments = [_comment(1, f"{BOT_USER_PREFIX}42]\nИван:\nСмотри !photo.jpg!")]
    attachments = {"photo.jpg": ("attachments/KAN-1/photo.jpg", True)}
    content = render_support_answer("KAN-1", "", "", comments, attachments)
    assert content is not None
    assert "![photo.jpg](attachments/KAN-1/photo.jpg)" in content
