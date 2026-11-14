from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_or_create_user


class RoleMiddleware(BaseMiddleware):
    """Подтягивает пользователя из БД и прокидывает data['user'] / data['role']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if from_user is None or from_user.is_bot or session is None:
            return await handler(event, data)

        user = await get_or_create_user(
            session,
            telegram_id=from_user.id,
            username=from_user.username,
            full_name=from_user.full_name,
        )
        if from_user.id in settings.admin_id_set:
            user.role = "admin"
            user.is_pending_manager = False

        data["user"] = user
        data["role"] = user.role
        return await handler(event, data)
