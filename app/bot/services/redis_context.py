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
            Redis.from_url(
                redis,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            if isinstance(redis, str)
            else redis
        )
        self.ttl = ttl
        self._memory: dict[str, str] = {}
        self._redis_failed = False

    @classmethod
    def from_url(cls, url: str, ttl: int | None = None) -> "RedisContextService":
        return cls(redis_url=url, ttl=ttl)

    @staticmethod
    def _key(user_id: int) -> str:
        return str(user_id)

    def _mark_redis_failed(self, exc: Exception) -> None:
        if not self._redis_failed:
            logger.warning("Redis недоступен, контекст хранится в памяти: %s", exc)
        self._redis_failed = True

    async def _read_raw(self, key: str) -> str | None:
        if not self._redis_failed:
            try:
                return await self.redis.get(key)
            except Exception as exc:  # Redis может быть временно недоступен
                self._mark_redis_failed(exc)
        return self._memory.get(key)

    async def get_context(self, user_id: int) -> list[dict]:
        raw = await self._read_raw(self._key(user_id))
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
        raw = json.dumps(context, ensure_ascii=False)
        self._memory[key] = raw
        if self._redis_failed:
            return
        try:
            await self.redis.set(key, raw)
            if self.ttl:
                await self.redis.expire(key, self.ttl)
        except Exception as exc:
            self._mark_redis_failed(exc)

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
        key = self._key(user_id)
        self._memory.pop(key, None)
        if self._redis_failed:
            return
        try:
            await self.redis.delete(key)
        except Exception as exc:
            self._mark_redis_failed(exc)
