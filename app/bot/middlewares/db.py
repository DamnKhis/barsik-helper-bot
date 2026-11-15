from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.exc import DBAPIError

from app.db import async_session_maker

logger = logging.getLogger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    """Открывает AsyncSession на один апдейт и кладёт её в data['session']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with async_session_maker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            try:
                await session.commit()
            except DBAPIError as exc:
                # Remote Postgres бывает нестабилен: не роняем обработку апдейта.
                await session.rollback()
                logger.warning("Не удалось сохранить изменения в БД: %s", exc)
                return None
            return result
