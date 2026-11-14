"""GET /health: статус API, Qdrant и Redis."""

from __future__ import annotations

import httpx
from fastapi import APIRouter
from redis.asyncio import Redis

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    services: dict[str, str] = {"api": "ok"}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
        services["qdrant"] = "ok" if response.status_code < 500 else "error"
    except Exception:
        services["qdrant"] = "error"

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.ping()
        services["redis"] = "ok"
    except Exception:
        services["redis"] = "error"
    finally:
        await redis.aclose()

    status = "ok" if all(value == "ok" for value in services.values()) else "degraded"
    return {"status": status, "services": services}
