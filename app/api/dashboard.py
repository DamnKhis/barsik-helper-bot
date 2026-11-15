"""Дашборд и агрегированные метрики GET /stats.

Период задаётся параметром ``?period=`` (today | week | month). Все границы и
подписи времени считаются в таймзоне Москвы (UTC+3, без перехода на летнее
время), чтобы «сегодня» у пользователя и на сервере совпадало.

Защита: если заданы DASHBOARD_USER / DASHBOARD_PASSWORD, страница и API требуют
HTTP Basic. Пустой пароль = защита выключена.
"""

from __future__ import annotations

import secrets
import statistics
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select

from app import db as db_module
from app.config import settings

router = APIRouter(tags=["dashboard"])

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "bars-dashboard.html"
BLIND_SPOT_THRESHOLD = 0.45

# Москва: UTC+3 круглый год (перехода на летнее время нет с 2014 года).
MSK = timezone(timedelta(hours=3))
PERIOD_DAYS = {"today": 1, "week": 7, "month": 30}
Period = Literal["today", "week", "month"]

_basic = HTTPBasic(auto_error=False)


def _require_dashboard_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """HTTP Basic, если пароль задан в настройках; иначе — открытый доступ."""
    if not settings.dashboard_password:
        return
    valid = creds is not None and secrets.compare_digest(
        creds.username, settings.dashboard_user
    ) and secrets.compare_digest(creds.password, settings.dashboard_password)
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": 'Basic realm="BARS dashboard"'},
        )


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


def _period_window(period: str) -> tuple[datetime, str]:
    """Начало периода (00:00 MSK) и гранулярность серии: hour для суток, day для недели/месяца."""
    days = PERIOD_DAYS.get(period, 1)
    now = datetime.now(MSK)
    start = datetime.combine(now.date() - timedelta(days=days - 1), time.min, tzinfo=MSK)
    return start, ("hour" if days == 1 else "day")


def _build_series(rows: list[Any], start: datetime, granularity: str) -> list[dict[str, Any]]:
    """Ряд нагрузки: 24 часа для суток, календарные дни для недели/месяца."""
    if granularity == "hour":
        buckets = {f"{hour:02d}": 0 for hour in range(24)}
        for row in rows:
            buckets[f"{_as_utc(row.ts).astimezone(MSK).hour:02d}"] += 1
        return [{"h": label, "c": count} for label, count in buckets.items()]

    now = datetime.now(MSK)
    days: dict[str, int] = {}
    cursor = start.date()
    while cursor <= now.date():
        days[cursor.strftime("%d.%m")] = 0
        cursor += timedelta(days=1)
    for row in rows:
        label = _as_utc(row.ts).astimezone(MSK).strftime("%d.%m")
        if label in days:
            days[label] += 1
    return [{"h": label, "c": count} for label, count in days.items()]


async def _collect_stats(period: str = "today") -> dict[str, Any]:
    start, granularity = _period_window(period)
    start_utc = start.astimezone(timezone.utc)

    async with db_module.async_session_maker() as session:
        all_rows = (
            await session.scalars(
                select(db_module.Request).order_by(
                    db_module.Request.ts.desc(),
                    db_module.Request.id.desc(),
                )
            )
        ).all()

    rows = [row for row in all_rows if _as_utc(row.ts) >= start_utc]
    total = len(rows)
    answered = sum(1 for row in rows if row.outcome == "answered")

    response_times = [int(row.response_ms or 0) for row in rows]
    avg_confidence = (
        sum(float(row.confidence or 0.0) for row in rows) / total if total else 0.0
    )

    topics: Counter[str] = Counter()
    for row in rows:
        topic = _topic_from_docs(row.docs or "")
        if topic:
            topics[topic] += 1

    time_format = "%H:%M" if granularity == "hour" else "%d.%m %H:%M"
    blind_spots = [
        {
            "time": _as_utc(row.ts).astimezone(MSK).strftime(time_format),
            "question": row.question,
            "confidence": round(float(row.confidence or 0.0), 3),
        }
        for row in rows
        if (row.confidence or 0.0) < BLIND_SPOT_THRESHOLD
    ][:5]

    now = datetime.now(MSK)
    return {
        "period": period,
        "tz": "Europe/Moscow",
        "range": {"from": start.isoformat(), "to": now.isoformat()},
        "granularity": granularity,
        "kpis": {
            "requests": total,
            "auto_rate": round(100 * answered / total, 1) if total else 0.0,
            "avg_confidence": round(avg_confidence, 3),
            "median_response_ms": int(statistics.median(response_times)) if response_times else 0,
        },
        "hourly": _build_series(rows, start, granularity),
        "outcomes": {
            "answered": answered,
            "not_found": sum(1 for row in rows if row.outcome == "not_found"),
            "escalated": sum(1 for row in rows if row.outcome == "escalated"),
        },
        "top_topics": [
            {"topic": topic, "count": count} for topic, count in topics.most_common(5)
        ],
        "blind_spots": blind_spots,
    }


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_dashboard_auth)],
)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))


@router.get("/stats", dependencies=[Depends(_require_dashboard_auth)])
async def stats(period: Period = "today") -> dict[str, Any]:
    return await _collect_stats(period)
