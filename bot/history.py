"""История диалога в Redis. Роль 3.

Ключ history:<user_id> -> JSON-список [{role, content}], последние MAX_MESSAGES.
История нужна для двух вещей: контекст в /ask и эскалация с полным диалогом
(требование брифа: оператор получает вопрос вместе с историей).
"""
from __future__ import annotations

import json

from redis.asyncio import Redis

MAX_MESSAGES = 12
TTL_SECONDS = 24 * 60 * 60


async def load_history(redis: Redis, user_id: int | str) -> list[dict]:
    raw = await redis.get(f"history:{user_id}")
    return json.loads(raw) if raw else []


async def save_history(redis: Redis, user_id: int | str, history: list[dict]) -> None:
    trimmed = history[-MAX_MESSAGES:]
    await redis.set(
        f"history:{user_id}",
        json.dumps(trimmed, ensure_ascii=False),
        ex=TTL_SECONDS,
    )
