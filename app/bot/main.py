"""Точка входа бота. Может запускаться вместе с FastAPI (lifespan) или отдельно."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from redis.asyncio import Redis

from app.bot.handlers import admin, common, manager, user
from app.bot.middlewares import DatabaseMiddleware, RoleMiddleware
from app.bot.services.redis_context import RedisContextService
from app.config import settings
from app.db import init_async_db

logger = logging.getLogger(__name__)


def create_bot() -> Bot:
    return Bot(token=settings.bot_token)


def create_dispatcher(redis_context: RedisContextService | None = None) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.outer_middleware(DatabaseMiddleware())
    dp.message.outer_middleware(RoleMiddleware())
    dp.callback_query.outer_middleware(DatabaseMiddleware())
    dp.callback_query.outer_middleware(RoleMiddleware())

    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(manager.router)
    dp.include_router(user.router)

    if redis_context is not None:
        dp.workflow_data["redis_context"] = redis_context
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    await init_async_db()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    redis_context = RedisContextService(redis)
    bot = create_bot()
    dp = create_dispatcher(redis_context)
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
