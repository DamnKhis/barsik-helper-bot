"""HTTP-клиент к RAG-ядру. Роль 3. Контракт — app/schemas.py, менять только вместе."""
from __future__ import annotations

import httpx

from settings import get_settings


class ApiError(RuntimeError):
    """RAG-ядро недоступно / вернуло ошибку."""


async def ask_api(question: str, user_id: str, history: list[dict]) -> dict:
    settings = get_settings()
    payload = {"text": question, "user_id": user_id, "history": history}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{settings.api_url}/ask", json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise ApiError(f"RAG-ядро недоступно: {exc}") from exc
