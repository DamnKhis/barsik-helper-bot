from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db as db_module
from app.api import dashboard as dashboard_module
from app.bot.handlers.user import _metric_outcome, handle_question
from app.bot.services.ai_service import AIResult


@pytest.fixture()
def session_maker(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}",
        poolclass=NullPool,
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(db_module.Request.__table__.create)

    asyncio.run(create_tables())
    monkeypatch.setattr(db_module, "async_session_maker", maker)

    yield maker
    asyncio.run(engine.dispose())


def _add_request(*args) -> None:
    asyncio.run(db_module.log_request(*args))


def _requests() -> list:
    async def fetch() -> list:
        async with db_module.async_session_maker() as session:
            return list(
                (
                    await session.scalars(
                        select(db_module.Request).order_by(db_module.Request.id)
                    )
                ).all()
            )

    return asyncio.run(fetch())


def test_log_request_persists_metrics(session_maker) -> None:
    _add_request(
        "42",
        "Как оформить командировку?",
        0.87,
        "answered",
        1234,
        "Командировки.pdf, стр. 3",
        777,
    )

    rows = _requests()
    assert len(rows) == 1
    assert rows[0].user_id == "42"
    assert rows[0].outcome == "answered"
    assert rows[0].confidence == pytest.approx(0.87)
    assert rows[0].response_ms == 1234
    assert rows[0].request_msg_id == 777


def test_collect_stats_aggregates_dashboard_contract(session_maker) -> None:
    _add_request("1", "Как оформить командировку?", 0.9, "answered", 1200, "Командировки.pdf, стр. 3; Приказ.pdf", 10)
    _add_request("1", "Где кнопка импорта?", 0.2, "escalated", 800, "", 11)
    _add_request("2", "Совсем непонятный вопрос", 0.1, "not_found", 500, "", 12)

    stats = asyncio.run(dashboard_module._collect_stats())

    assert stats["period"] == "today"
    assert stats["tz"] == "Europe/Moscow"
    assert stats["granularity"] == "hour"
    assert stats["kpis"] == {
        "requests": 3,
        "auto_rate": 33.3,
        "avg_confidence": 0.4,
        "median_response_ms": 800,
    }
    assert stats["outcomes"] == {"answered": 1, "not_found": 1, "escalated": 1}
    assert len(stats["hourly"]) == 24
    assert sum(point["c"] for point in stats["hourly"]) == 3
    assert stats["top_topics"] == [{"topic": "Командировки.pdf", "count": 1}]
    assert [spot["question"] for spot in stats["blind_spots"]] == [
        "Совсем непонятный вопрос",
        "Где кнопка импорта?",
    ]
    assert all(spot["confidence"] < 0.45 for spot in stats["blind_spots"])


def test_period_switches_granularity_and_window(session_maker) -> None:
    async def add_older_request() -> None:
        async with db_module.async_session_maker() as session:
            session.add(
                db_module.Request(
                    ts=datetime.now(timezone.utc) - timedelta(days=3),
                    user_id="9",
                    question="Старый вопрос за прошлую неделю",
                    confidence=0.9,
                    outcome="answered",
                    response_ms=1000,
                    docs="Командировки.pdf",
                )
            )
            await session.commit()

    _add_request("1", "Вопрос сегодня", 0.8, "answered", 1000, "Командировки.pdf", 1)
    asyncio.run(add_older_request())

    today = asyncio.run(dashboard_module._collect_stats("today"))
    assert today["kpis"]["requests"] == 1
    assert today["granularity"] == "hour"

    week = asyncio.run(dashboard_module._collect_stats("week"))
    assert week["kpis"]["requests"] == 2
    assert week["granularity"] == "day"
    assert len(week["hourly"]) == 7
    assert sum(point["c"] for point in week["hourly"]) == 2


def test_metric_outcome_mapping() -> None:
    assert _metric_outcome(AIResult(answer="ok", flag="ANSWER")) == "answered"
    assert _metric_outcome(AIResult(answer="уточните", flag="CLARIFY")) == "not_found"
    assert _metric_outcome(AIResult(answer="оператор", flag="ESCALATE", need_operator=True)) == "escalated"


def test_bot_handler_writes_metrics_after_answer(session_maker) -> None:
    class FakeMessage:
        text = "Как оформить командировку?"
        caption = None
        photo = None
        document = None

        def __init__(self) -> None:
            self.sent: list[tuple[str, object]] = []

        async def answer(self, text: str, reply_markup=None):
            self.sent.append((text, reply_markup))
            return SimpleNamespace(message_id=555)

    class FakeState:
        def __init__(self) -> None:
            self.data: dict = {}

        async def update_data(self, **kwargs) -> None:
            self.data.update(kwargs)

    class FakeRedisContext:
        async def get_context(self, user_id: int) -> list:
            return []

        async def append_pair(self, user_id: int, question: str, answer: str) -> None:
            pass

    class FakeAIService:
        async def answer(self, user_id: int, question: str, context: list, image_path=None, bot_username=None) -> AIResult:
            return AIResult(
                answer="Нужен оператор, чтобы помочь с командировкой.",
                flag="ESCALATE",
                need_operator=True,
                source="Командировки.pdf",
                confidence=0.31,
            )

    class FakeBot:
        async def send_chat_action(self, *args, **kwargs) -> None:
            pass

    message = FakeMessage()
    state = FakeState()
    asyncio.run(
        handle_question(
            message,
            state,
            session=None,
            user=SimpleNamespace(telegram_id=42),
            redis_context=FakeRedisContext(),
            ai_service=FakeAIService(),
            bot=FakeBot(),
        )
    )

    assert len(message.sent) == 1
    assert message.sent[0][1] is not None
    rows = _requests()
    assert len(rows) == 1
    assert rows[0].outcome == "escalated"
    assert rows[0].confidence == pytest.approx(0.31)
    assert rows[0].docs == "Командировки.pdf"
    assert rows[0].request_msg_id == 555
    assert rows[0].response_ms >= 0


def test_stats_endpoint_returns_dashboard_payload(session_maker) -> None:
    _add_request("1", "Вопрос", 0.8, "answered", 900, "Документ.pdf", 1)

    from app.main import app

    response = TestClient(app).get("/stats?period=today")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "period",
        "tz",
        "range",
        "granularity",
        "kpis",
        "hourly",
        "outcomes",
        "top_topics",
        "blind_spots",
    }
    assert body["period"] == "today"
    assert body["kpis"]["requests"] == 1
    assert body["outcomes"]["answered"] == 1
