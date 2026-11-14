"""RAG-ядро: FastAPI со всеми эндпоинтами. Роль 2 (+ роль 4: аналитика, вебхуки).

Эндпоинты:
  POST /ask            — вопрос -> ответ с источниками и уверенностью (app/schemas.py)
  GET  /stats          — агрегаты для дашборда
  GET  /dashboard      — HTML-дашборд (Chart.js)
  GET  /health         — статус зависимостей
  POST /webhook/github — пуш в GitHub -> реиндексация изменённых документов
  POST /webhook/gitlab — то же для GitLab

Запуск: uvicorn app.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.analytics.db import guess_topic, init_db, log_query
from app.analytics.stats import get_stats
from app.integrations.git_provider import (
    extract_github_push,
    extract_gitlab_push,
    verify_github_signature,
)
from app.integrations.reindex import reindex_files, start_poller
from app.rag.confidence import estimate_confidence, should_escalate
from app.rag.generator import Generator
from app.rag.retriever import Retriever
from app.schemas import AskRequest, AskResponse, Source
from settings import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    init_db(settings.db_path)
    application.state.retriever = Retriever(settings)
    application.state.generator = Generator(settings)
    application.state.poller = start_poller(settings)
    logger.info("RAG-ядро готово: LLM=%s, Qdrant=%s", settings.llm_model, settings.qdrant_url)
    yield
    if application.state.poller:
        application.state.poller.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="BARS Support Bot API", version="0.1.0", lifespan=lifespan)

    @app.post("/ask", response_model=AskResponse)
    async def ask(req: AskRequest) -> AskResponse:
        settings = get_settings()
        started = time.perf_counter()

        chunks = await app.state.retriever.retrieve(req.text)
        answer = await app.state.generator.generate(req.text, chunks, [m.model_dump() for m in req.history])
        confidence = estimate_confidence(chunks)
        escalate = should_escalate(answer, confidence, settings)
        latency_ms = int((time.perf_counter() - started) * 1000)

        await asyncio.to_thread(
            log_query,
            settings.db_path,
            str(req.user_id),
            req.text,
            guess_topic(req.text),
            confidence,
            not escalate,
            escalate,
            latency_ms,
        )
        return AskResponse(
            answer=answer,
            sources=[
                Source(doc=c.doc, section=c.section_path, link=c.link, score=round(c.score, 3))
                for c in chunks
            ],
            confidence=round(confidence, 3),
            escalate=escalate,
        )

    @app.get("/stats")
    async def stats() -> dict:
        return get_stats(get_settings())

    @app.get("/dashboard")
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))

    @app.get("/health")
    async def health() -> dict:
        settings = get_settings()
        qdrant_ok = False
        try:
            qdrant_ok = await app.state.retriever.qdrant.collection_exists(settings.qdrant_collection)
        except Exception:  # noqa: BLE001 — health-эндпоинт не должен падать
            pass
        return {
            "status": "ok",
            "qdrant_collection_ready": qdrant_ok,
            "llm_configured": bool(settings.llm_api_key),
        }

    @app.post("/webhook/github")
    async def webhook_github(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> dict:
        settings = get_settings()
        body = await request.body()
        if not verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret):
            raise HTTPException(status_code=403, detail="Подпись не совпала")
        if x_github_event != "push":
            return {"status": "ignored", "reason": f"event={x_github_event}"}
        push = extract_github_push(json.loads(body))
        if push["branch"] != settings.docs_repo_branch or not push["files"]:
            return {"status": "ignored", "reason": "нет изменений в документации"}
        result = await asyncio.to_thread(reindex_files, settings, push["files"], "github")
        return {"status": "ok", "result": result}

    @app.post("/webhook/gitlab")
    async def webhook_gitlab(
        request: Request,
        x_gitlab_token: str | None = Header(default=None),
        x_gitlab_event: str | None = Header(default=None),
    ) -> dict:
        settings = get_settings()
        if settings.gitlab_webhook_secret and x_gitlab_token != settings.gitlab_webhook_secret:
            raise HTTPException(status_code=403, detail="Токен не совпал")
        if x_gitlab_event != "Push Hook":
            return {"status": "ignored", "reason": f"event={x_gitlab_event}"}
        push = extract_gitlab_push(json.loads(await request.body()))
        if push["branch"] != settings.docs_repo_branch or not push["files"]:
            return {"status": "ignored", "reason": "нет изменений в документации"}
        result = await asyncio.to_thread(reindex_files, settings, push["files"], "gitlab")
        return {"status": "ok", "result": result}

    return app


app = create_app()
