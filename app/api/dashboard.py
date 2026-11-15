"""Дашборд и агрегированные метрики GET /stats."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app import db as db_module

router = APIRouter(tags=["dashboard"])

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "bars-dashboard.html"
BLIND_SPOT_THRESHOLD = 0.45


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _topic_from_docs(docs: str) -> str | None:
    first = docs.split(";", 1)[0].strip()
    if first.lower().startswith("источник:"):
        first = first.split(":", 1)[1].strip()
    first = first.split(",", 1)[0].strip()
    return first or None


async def _collect_stats() -> dict[str, Any]:
    async with db_module.async_session_maker() as session:
        rows = (
            await session.scalars(
                select(db_module.Request).order_by(
                    db_module.Request.ts.desc(),
                    db_module.Request.id.desc(),
                )
            )
        ).all()

    now = datetime.now(timezone.utc)
    today_rows = [row for row in rows if _as_utc(row.ts).date() == now.date()]
    total = len(today_rows)
    answered = sum(1 for row in today_rows if row.outcome == "answered")

    hourly_counts = {f"{hour:02d}": 0 for hour in range(24)}
    cutoff = now - timedelta(hours=24)
    for row in rows:
        ts = _as_utc(row.ts)
        if ts >= cutoff:
            hourly_counts[f"{ts.astimezone().hour:02d}"] += 1

    topics: Counter[str] = Counter()
    for row in rows:
        topic = _topic_from_docs(row.docs or "")
        if topic:
            topics[topic] += 1

    blind_spots = [
        {
            "time": _as_utc(row.ts).astimezone().strftime("%H:%M"),
            "question": row.question,
            "confidence": round(float(row.confidence or 0.0), 3),
        }
        for row in rows
        if (row.confidence or 0.0) < BLIND_SPOT_THRESHOLD
    ][:5]

    avg_confidence = (
        sum(float(row.confidence or 0.0) for row in today_rows) / total if total else 0.0
    )
    avg_response_ms = (
        sum(int(row.response_ms or 0) for row in today_rows) / total if total else 0
    )

    return {
        "kpis": {
            "requests_today": total,
            "auto_rate": round(100 * answered / total, 1) if total else 0.0,
            "avg_confidence": round(avg_confidence, 3),
            "avg_response_ms": int(avg_response_ms),
        },
        "hourly": [{"h": hour, "c": count} for hour, count in hourly_counts.items()],
        "outcomes": {
            "answered": answered,
            "not_found": sum(1 for row in today_rows if row.outcome == "not_found"),
            "escalated": sum(1 for row in today_rows if row.outcome == "escalated"),
        },
        "top_topics": [
            {"topic": topic, "count": count}
            for topic, count in topics.most_common(5)
        ],
        "blind_spots": blind_spots,
    }


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))


@router.get("/stats")
async def stats() -> dict[str, Any]:
    return await _collect_stats()
