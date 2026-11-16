from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.handlers import user as user_module


def test_operator_request_detects_word_forms() -> None:
    assert user_module._is_operator_request("позови оператора")
    assert user_module._is_operator_request("нужен живой человек")
    assert user_module._is_operator_request("соедините со специалистом")
    assert user_module._is_operator_request("вызовите менеджера")
    assert user_module._is_operator_request("нужна поддержка")
    assert user_module._is_operator_request("позовите человека пожалуйста")


def test_operator_request_ignores_normal_questions() -> None:
    assert not user_module._is_operator_request("как оформить командировку?")
    assert not user_module._is_operator_request("где кнопка импорта")


def test_offer_operator_sends_button() -> None:
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock())

    asyncio.run(user_module._offer_operator(message, state, "позови оператора"))

    assert message.answer.await_count == 1
    kwargs = message.answer.await_args.kwargs
    assert kwargs.get("reply_markup") is not None
    state.update_data.assert_awaited_once()
