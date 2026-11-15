"""Тесты обработки вопросов со скриншотами (без сети и без Telegram)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import user as user_module
from app.bot.services import ai_service as ai_service_module
from app.bot.services import vision as vision_module
from app.bot.services.ai_service import AIService
from app.bot.services.image_store import guess_suffix, is_image_message, save_query_image
from app.bot.services.vision import ImageUnderstanding, build_query

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


def _understanding(**overrides) -> ImageUnderstanding:
    data = {
        "ok": True,
        "kind": "screenshot",
        "description": "Диалоговое окно «Просмотр зависимых документов».",
        "ocr_text": "Скрыть Нижняя панель Действия документа",
        "ui_elements": [{"label": "Кнопка «Сохранить»", "location": "внизу справа"}],
        "questions": ["Как настроить расположение зависимых документов?"],
    }
    data.update(overrides)
    return ImageUnderstanding(**data)


def _photo_message(chat_id: int = 1, caption: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        photo=[SimpleNamespace(file_id="small", file_unique_id="u1"), SimpleNamespace(file_id="big", file_unique_id="u2")],
        document=None,
        caption=caption,
        text=None,
        chat=SimpleNamespace(id=chat_id),
        answer=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=555)),
        answer_photo=AsyncMock(),
    )


def _text_message(text: str, chat_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        photo=None,
        document=None,
        caption=None,
        text=text,
        chat=SimpleNamespace(id=chat_id),
        answer=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=777)),
        answer_photo=AsyncMock(),
    )


def _bot() -> SimpleNamespace:
    async def download(_file, destination: Path) -> None:
        Path(destination).write_bytes(PNG_BYTES)

    return SimpleNamespace(
        download=AsyncMock(side_effect=download),
        send_chat_action=AsyncMock(),
    )


class _FakeRetriever:
    def __init__(self, images: list[str] | None = None) -> None:
        self.queries: list[str] = []
        self._images = images or []

    async def answer_query(self, query: str):
        self.queries.append(query)
        return SimpleNamespace(
            answer="Ответ по документации",
            flag="ANSWER",
            need_operator=False,
            source="Функциональные возможности, 2.3.1 (стр. 12)",
            confidence=0.9,
            images=list(self._images),
        )


def _service_with(retriever: _FakeRetriever) -> AIService:
    service = AIService()
    service._retriever = retriever
    service._init_attempted = True
    return service


# --- нормализация запроса -------------------------------------------------


def test_text_query_is_unchanged() -> None:
    """Текстовый путь обязан остаться байт-в-байт прежним."""
    assert build_query("Почему не работает фильтр !> 200?", None) == "Почему не работает фильтр !> 200?"


def test_image_query_mirrors_screenshot_chunk_layout() -> None:
    query = build_query("что тут настроить?", _understanding())
    assert query.startswith("Скриншот интерфейса БАРС-Офис. Вопрос пользователя: что тут настроить?")
    assert "Изображение: что тут настроить?" in query
    assert "Диалоговое окно «Просмотр зависимых документов»." in query
    assert "Текст на изображении:\nСкрыть Нижняя панель" in query
    assert "Элементы интерфейса:\n- Кнопка «Сохранить» (внизу справа)" in query
    assert "Вопросы: Как настроить расположение зависимых документов?" in query


def test_image_query_hints_domain_without_caption() -> None:
    query = build_query("", _understanding())
    assert "БАРС-Офис" in query  # доменный якорь: фильтр оффтопа не сработает
    assert "не указан" in query


def test_image_query_is_capped() -> None:
    query = build_query("вопрос", _understanding(description="д" * 5000, ocr_text="т" * 5000))
    assert len(query) <= vision_module.MAX_QUERY_CHARS


def test_failed_vision_falls_back_to_caption() -> None:
    assert build_query("не открывается окно", ImageUnderstanding.failed("timeout")) == (
        "Изображение: не открывается окно (скриншот интерфейса БАРС-Офис)."
    )
    assert build_query("", ImageUnderstanding.failed("timeout")) == ""


def test_vision_disabled_is_not_available(monkeypatch) -> None:
    monkeypatch.setattr(vision_module.settings, "vision_enabled", False)
    assert vision_module.vision_available() is False


# --- AIService ------------------------------------------------------------


def test_ai_service_sends_image_query_to_retriever(tmp_path, monkeypatch) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    monkeypatch.setattr(
        vision_module,
        "describe_image",
        lambda path, caption="": _understanding(),
    )
    retriever = _FakeRetriever()
    service = _service_with(retriever)

    result = asyncio.run(service.answer(10, "что тут?", [], image_path=image))

    assert retriever.queries and "Текст на изображении:" in retriever.queries[0]
    assert result.answer == "Ответ по документации"
    assert result.source.startswith("Функциональные возможности")


def test_ai_service_text_question_untouched(monkeypatch) -> None:
    retriever = _FakeRetriever()
    service = _service_with(retriever)

    asyncio.run(service.answer(10, "Как оформить командировку?", []))

    assert retriever.queries == ["Как оформить командировку?"]


def test_ai_service_asks_for_text_when_image_unreadable(tmp_path, monkeypatch) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    monkeypatch.setattr(vision_module, "describe_image", lambda path, caption="": ImageUnderstanding.failed("нет ключа"))
    retriever = _FakeRetriever()
    service = _service_with(retriever)

    result = asyncio.run(service.answer(10, "", [], image_path=image))

    assert result.flag == "CLARIFY"
    assert retriever.queries == []
    assert "текстом" in result.answer


def test_kb_images_are_opt_in(monkeypatch) -> None:
    images = [str(Path("/tmp/shot.png"))]
    monkeypatch.setattr(ai_service_module.settings, "send_kb_images", False)
    assert asyncio.run(_service_with(_FakeRetriever(images=images)).answer(10, "вопрос", [])).images == []

    monkeypatch.setattr(ai_service_module.settings, "send_kb_images", True)
    assert asyncio.run(_service_with(_FakeRetriever(images=images)).answer(10, "вопрос", [])).images == images


# --- раскладка файлов -----------------------------------------------------


def test_guess_suffix_by_mime_and_name() -> None:
    assert guess_suffix("image/png") == ".png"
    assert guess_suffix("image/jpeg") == ".jpg"
    assert guess_suffix(None, "screen.jpeg") == ".jpg"
    assert guess_suffix("application/pdf", "doc.pdf") is None


def test_is_image_message_detects_photo_and_file() -> None:
    assert is_image_message(_photo_message()) is True
    assert is_image_message(SimpleNamespace(photo=None, document=None)) is False
    assert is_image_message(
        SimpleNamespace(photo=None, document=SimpleNamespace(mime_type="image/png", file_name="s.png"))
    ) is True
    assert is_image_message(
        SimpleNamespace(photo=None, document=SimpleNamespace(mime_type="application/pdf", file_name="d.pdf"))
    ) is False


def test_save_query_image_keeps_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.bot.services.image_store.settings.image_query_dir", str(tmp_path))
    path = asyncio.run(save_query_image(_bot(), _photo_message(), user_id=42))

    assert path.is_file()
    assert path.read_bytes() == PNG_BYTES
    assert path.parent.name == "42"
    assert path.suffix == ".jpg"  # фото Telegram всегда JPEG


def test_save_query_image_rejects_too_large(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.bot.services.image_store.settings.image_query_dir", str(tmp_path))
    monkeypatch.setattr("app.bot.services.image_store.max_image_bytes", lambda: 10)
    with pytest.raises(ValueError):
        asyncio.run(save_query_image(_bot(), _photo_message(), user_id=42))
    assert list(tmp_path.rglob("*.jpg")) == []


# --- хэндлер --------------------------------------------------------------


@pytest.fixture()
def no_db(monkeypatch):
    """Хэндлер не должен ходить в реальную БД во время тестов."""
    monkeypatch.setattr(user_module, "log_request", AsyncMock())
    monkeypatch.setattr(user_module, "create_dialog_entry", AsyncMock())


def test_handler_processes_photo(tmp_path, monkeypatch, no_db) -> None:
    monkeypatch.setattr("app.bot.services.image_store.settings.image_query_dir", str(tmp_path))
    monkeypatch.setattr(vision_module, "describe_image", lambda path, caption="": _understanding())

    seen: dict = {}

    class _Service:
        async def answer(self, user_id, question, context, image_path=None):
            seen["question"] = question
            seen["image_path"] = image_path
            return user_module.AIResult(answer="Ответ по скриншоту", flag="ANSWER", source="БП Командировка")

    message = _photo_message(caption="что это за окно?")
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
    user = SimpleNamespace(telegram_id=42)
    redis_context = SimpleNamespace(
        get_context=AsyncMock(return_value=[]),
        append_pair=AsyncMock(),
    )
    session = SimpleNamespace()

    asyncio.run(
        user_module.handle_question(
            message, state, session, user, redis_context, _Service(), _bot()
        )
    )

    assert seen["question"] == "что это за окно?"
    assert seen["image_path"] is not None and Path(seen["image_path"]).is_file()
    assert message.answer.await_args.args[0] == "Ответ по скриншоту"


def test_handler_text_question_has_no_image(tmp_path, monkeypatch, no_db) -> None:
    monkeypatch.setattr("app.bot.services.image_store.settings.image_query_dir", str(tmp_path))
    seen: dict = {}

    class _Service:
        async def answer(self, user_id, question, context, image_path=None):
            seen["image_path"] = image_path
            return user_module.AIResult(answer="Ответ", flag="ANSWER")

    message = _text_message("Как оформить командировку?")
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
    user = SimpleNamespace(telegram_id=42)
    redis_context = SimpleNamespace(get_context=AsyncMock(return_value=[]), append_pair=AsyncMock())

    asyncio.run(user_module.handle_question(message, state, SimpleNamespace(), user, redis_context, _Service(), _bot()))

    assert seen["image_path"] is None
    assert not list(tmp_path.rglob("*"))  # ничего не сохраняли


def test_handler_masks_image_only_question_placeholder(tmp_path, monkeypatch, no_db) -> None:
    monkeypatch.setattr("app.bot.services.image_store.settings.image_query_dir", str(tmp_path))
    monkeypatch.setattr(vision_module, "describe_image", lambda path, caption="": _understanding())
    seen: dict = {}

    class _Service:
        async def answer(self, user_id, question, context, image_path=None):
            seen["question"] = question
            return user_module.AIResult(answer="Ответ", flag="ANSWER")

    message = _photo_message(caption=None)
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
    user = SimpleNamespace(telegram_id=42)
    redis_context = SimpleNamespace(get_context=AsyncMock(return_value=[]), append_pair=AsyncMock())

    asyncio.run(user_module.handle_question(message, state, SimpleNamespace(), user, redis_context, _Service(), _bot()))

    assert seen["question"] == user_module.IMAGE_PLACEHOLDER


def test_send_long_splits_telegram_limit() -> None:
    text = "\n\n".join("блок " + str(i) + " " + "x" * 400 for i in range(30))
    message = SimpleNamespace(answer=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=1)))

    sent = asyncio.run(user_module._send_long(message, text))

    assert message.answer.await_count > 1
    for call in message.answer.await_args_list:
        assert len(call.args[0]) <= user_module.TELEGRAM_CHUNK
    assert sent.message_id == 1


def test_send_long_keeps_text_of_one_huge_block() -> None:
    """Абзац длиннее лимита режется, но текст не теряется (правим падение message is too long)."""
    text = "y" * (user_module.TELEGRAM_CHUNK * 2 + 5)
    message = SimpleNamespace(answer=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=2)))

    asyncio.run(user_module._send_long(message, text))

    joined = "".join(call.args[0] for call in message.answer.await_args_list)
    assert joined == text
