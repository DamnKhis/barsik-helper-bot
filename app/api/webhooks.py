"""Вебхуки Git-провайдеров.

Сами провайдеры и переиндексация лежат в app/integrators/ и принадлежат
другой команде. Здесь — только приём запроса и подтверждение.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


async def _payload(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    payload = await _payload(request)
    logger.info("GitHub webhook: event=%s", request.headers.get("X-GitHub-Event"))
    return {
        "status": "accepted",
        "provider": "github",
        "repository": (payload.get("repository") or {}).get("full_name"),
    }


@router.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request) -> dict:
    payload = await _payload(request)
    logger.info("GitLab webhook: event=%s", request.headers.get("X-Gitlab-Event"))
    return {
        "status": "accepted",
        "provider": "gitlab",
        "project": (payload.get("project") or {}).get("path_with_namespace"),
    }
