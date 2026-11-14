"""Контекст диалога для ИИ в Redis.

Ключ — telegram_id пользователя, значение — JSON-список сообщений вида
``[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]``.
"""

from __future__ import annotations

import json
import logging

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RedisContextService:
    def __init__(
        self,
        redis: Redis | str | None = None,
        ttl: int | None = None,
        redis_url: str | None = None,
    ) -> None:
        if redis is None:
            redis = redis_url
        if redis is None:
            raise ValueError("Нужен Redis-клиент или redis_url")
        self.redis: Redis = (
            Redis.from_url(redis, decode_responses=True) if isinstance(redis, str) else redis
        )
        self.ttl = ttl

    @classmethod
    def from_url(cls, url: str, ttl: int | None = None) -> "RedisContextService":
        return cls(redis_url=url, ttl=ttl)

    @staticmethod
    def _key(user_id: int) -> str:
        return str(user_id)

    async def get_context(self, user_id: int) -> list[dict]:
        try:
            raw = await self.redis.get(self._key(user_id))
        except Exception as exc:  # Redis может быть временно недоступен
            logger.error("Не удалось прочитать контекст user_id=%s: %s", user_id, exc)
            return []
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Битый JSON в контексте user_id=%s, сбрасываю", user_id)
            return []
        return data if isinstance(data, list) else []

    async def _save(self, user_id: int, context: list[dict]) -> None:
        key = self._key(user_id)
        try:
            await self.redis.set(key, json.dumps(context, ensure_ascii=False))
            if self.ttl:
                await self.redis.expire(key, self.ttl)
        except Exception as exc:
            logger.error("Не удалось сохранить контекст user_id=%s: %s", user_id, exc)

    async def append_pair(self, user_id: int, question: str, answer: str) -> None:
        context = await self.get_context(user_id)
        context.append({"role": "user", "content": question})
        context.append({"role": "assistant", "content": answer})
        await self._save(user_id, context)

    async def append_message(self, user_id: int, role: str, content: str) -> None:
        context = await self.get_context(user_id)
        context.append({"role": role, "content": content})
        await self._save(user_id, context)

    async def append_answer(self, user_id: int, answer: str) -> None:
        await self.append_message(user_id, "assistant", answer)

    async def clear_context(self, user_id: int) -> None:
        try:
            await self.redis.delete(self._key(user_id))
        except Exception as exc:
            logger.error("Не удалось очистить контекст user_id=%s: %s", user_id, exc)
