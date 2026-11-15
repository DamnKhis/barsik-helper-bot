from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.handlers import user as user_module
from app.bot.keyboards.reply import BTN_ASK, user_main_menu


def _user(pending: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        is_pending_manager=pending,
        telegram_id=123,
        full_name="Test User",
        username="test_user",
    )


def test_user_menu_has_no_access_request_button() -> None:
    texts = [button.text for row in user_main_menu().keyboard for button in row]
    assert texts == [BTN_ASK]
    assert all("права менеджера" not in text for text in texts)


def test_hidden_command_requests_access_without_reply(monkeypatch) -> None:
    notify = AsyncMock()
    monkeypatch.setattr(user_module, "notify_admins_manager_request", notify)
    user = _user()
    session = SimpleNamespace(flush=AsyncMock())
    bot = object()
    message = SimpleNamespace(answer=AsyncMock())

    asyncio.run(user_module.request_access(message, session, user, bot))

    assert user.is_pending_manager is True
    session.flush.assert_awaited_once()
    notify.assert_awaited_once_with(bot, session, user)
    message.answer.assert_not_awaited()


def test_hidden_command_is_silent_for_pending_user(monkeypatch) -> None:
    notify = AsyncMock()
    monkeypatch.setattr(user_module, "notify_admins_manager_request", notify)
    user = _user(pending=True)
    session = SimpleNamespace(flush=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock())

    asyncio.run(user_module.request_access(message, session, user, object()))

    session.flush.assert_not_awaited()
    notify.assert_not_awaited()
    message.answer.assert_not_awaited()
