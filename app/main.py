"""Единая точка запуска: FastAPI + aiogram polling в одном процессе."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.api import dashboard, health, webhooks
from app.bot.main import create_bot, create_dispatcher
from app.bot.services.ai_service import AIService
from app.bot.services.redis_context import RedisContextService
from app.config import settings
from app.db import engine, init_async_db, init_db
from app.kb_autosync import start as start_kb_autosync
from app.kb_autosync import stop as stop_kb_autosync

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_async_db()

    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    redis_context = RedisContextService(redis)
    ai_service = AIService()
    app.state.redis = redis
    app.state.redis_context = redis_context
    app.state.ai_service = ai_service
    app.state.polling_task = None
    app.state.bot = None
    app.state.dispatcher = None
    # Локальное автообновление bars-kb/dist: отдельный процесс не нужен.
    app.state.kb_autosync = await start_kb_autosync()

    if settings.bot_token:
        bot = create_bot()
        dp = create_dispatcher(redis_context, ai_service)
        app.state.bot = bot
        app.state.dispatcher = dp
        app.state.polling_task = asyncio.create_task(
            dp.start_polling(bot, skip_updates=True, handle_signals=False),
            name="aiogram-polling",
        )
        logger.info("Telegram polling запущен")
    else:
        logger.warning("BOT_TOKEN не задан — API работает без Telegram-бота")

    try:
        yield
    finally:
        task = app.state.polling_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if app.state.bot is not None:
            await app.state.bot.session.close()
        if app.state.dispatcher is not None:
            await app.state.dispatcher.storage.close()
        await stop_kb_autosync(app.state.kb_autosync)
        await redis.aclose()
        await engine.dispose()
        logger.info("Приложение остановлено")


app = FastAPI(title="БАРС-Офис helper bot", lifespan=lifespan)

# Локальные статики дашборда (Chart.js): работают без интернета.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(dashboard.router)
