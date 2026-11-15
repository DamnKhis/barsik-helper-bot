"""Слой данных бота «БАРС-Офис» — целиком на SQLAlchemy (async).

Всё работает через один движок и один DSN — переменная окружения DATABASE_URL
(для PostgreSQL: postgresql+asyncpg://barsik:<пароль>@postgres:5432/barsik).

Здесь живут две независимые части:

1. Метрики (``requests`` / ``reindex_log``): их пишет бот после каждого ответа
   (log_request, set_rating), читает GET /stats; реиндексации пишет
   git-интеграция (log_reindex). Функции асинхронные — вызывать через ``await``,
   asyncio.to_thread НЕ нужен.
2. Пользователи и история (``users`` / ``dialog_history`` / ``jira_sessions``):
   ORM-модели, сессия приходит в хендлеры через DatabaseMiddleware (data['session']).

Расчёт метрик и эндпоинт /stats — зона ответственности другой команды,
эти функции менять не нужно: таблицы и поля сохранены 1:1.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema

from app.config import settings

logger = logging.getLogger(__name__)

OUTCOMES = ("answered", "not_found", "escalated")


def _postgres_schema() -> str | None:
    if not settings.database_url.startswith("postgres"):
        return None
    schema = settings.postgres_schema.strip()
    return schema or None


DB_SCHEMA = _postgres_schema()
ENGINE_CONNECT_ARGS = (
    {"server_settings": {"search_path": f"{DB_SCHEMA},public"}} if DB_SCHEMA else {}
)


async def _ensure_schema(conn) -> None:
    if DB_SCHEMA:
        await conn.execute(CreateSchema(DB_SCHEMA, if_not_exists=True))


class Base(DeclarativeBase):
    """Общий декларативный базовый класс всех моделей (метрики + пользователи)."""


# ---------------------------------------------------------------------------
# ORM-модели метрик (таблицы requests / reindex_log — прежние имена и поля)
# ---------------------------------------------------------------------------


class Request(Base):
    """Один вопрос-ответ: строка таблицы ``requests`` для дашборда."""

    __tablename__ = "requests"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('answered', 'not_found', 'escalated')",
            name="ck_requests_outcome",
        ),
        Index("idx_requests_ts", "ts"),
        Index("idx_requests_out", "outcome"),
        Index("idx_requests_msg", "user_id", "request_msg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    response_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    docs: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ReindexLog(Base):
    """Запись о реиндексации базы знаний (git-интеграция)."""

    __tablename__ = "reindex_log"
    __table_args__ = (Index("idx_reindex_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    files_changed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Метрики: публичные функции (вызывает бот и git-интеграция)
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Создать таблицы метрик requests/reindex_log (идемпотентно).

    Вызывается на старте приложения (lifespan в app/main.py).
    """
    if settings.database_url.startswith("sqlite"):
        raw_path = settings.database_url.split(":///", 1)[-1]
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await _ensure_schema(conn)
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Request.__table__, ReindexLog.__table__],
        )


async def log_request(
    user_id: str,
    question: str,
    confidence: float,
    outcome: str,
    response_ms: int = 0,
    docs: str = "",
    msg_id: int | None = None,
) -> None:
    """Записать один вопрос-ответ. Бот вызывает сразу после отправки ответа.

    outcome:  'answered' | 'not_found' | 'escalated'
    docs:     источники через '; ' — "Руководство HR.pdf, стр. 14; Приказ 27.pdf"
    msg_id:   id сообщения бота с ответом — по нему позже привяжется 👍/👎

    Ошибка БД не роняет бота: пишем в лог и продолжаем отвечать.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome должен быть одним из {OUTCOMES}, получено: {outcome!r}")
    try:
        async with async_session_maker() as session:
            session.add(
                Request(
                    user_id=str(user_id),
                    question=question,
                    confidence=float(confidence),
                    outcome=outcome,
                    response_ms=int(response_ms),
                    docs=docs,
                    request_msg_id=msg_id,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.error("Не удалось записать запрос в БД: %s", exc)


async def set_rating(user_id: str, msg_id: int, rating: int) -> bool:
    """Привязать 👍 (1) / 👎 (-1) к ответу бота. Бот вызывает из обработчика кнопки.

    Ищем по паре (user_id, msg_id): в Telegram id сообщений уникальны внутри
    чата, поэтому пара однозначно определяет строку. Возвращает True, если
    строка нашлась (False — нет такой записи или сбой БД).
    """
    if rating not in (1, -1):
        raise ValueError(f"rating должен быть 1 или -1, получено: {rating!r}")
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                update(Request)
                .where(
                    Request.user_id == str(user_id),
                    Request.request_msg_id == int(msg_id),
                )
                .values(rating=rating)
            )
            await session.commit()
            return result.rowcount > 0
    except Exception as exc:
        logger.error("Не удалось сохранить оценку: %s", exc)
        return False


async def log_reindex(source: str, files_changed: int, status: str, details: str = "") -> None:
    """Лог реиндексаций для git-интеграции: какой webhook/poller и сколько файлов обновил."""
    try:
        async with async_session_maker() as session:
            session.add(
                ReindexLog(
                    source=source,
                    files_changed=int(files_changed),
                    status=status,
                    details=details,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.error("Не удалось записать лог реиндексации: %s", exc)


# ---------------------------------------------------------------------------
# SQLAlchemy async: пользователи, история и Jira
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    # Легаси-колонка: оставлена для совместимости с уже созданной БД.
    is_pending_manager: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DialogHistory(Base):
    __tablename__ = "dialog_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    # Легаси-колонки тикетов: новые записи всегда получают безопасные значения.
    ticket_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False, index=True)
    manager_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class JiraSession(Base):
    """Связь Telegram-пользователя с задачей Jira."""

    __tablename__ = "jira_sessions"
    __table_args__ = (Index("idx_jira_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    issue_key: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    issue_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    last_comment_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Remote Postgres рвёт соединения по простою, поэтому для него не кэшируем коннекты.
# SQLite остаётся на обычном пуле (файл, сброс неактуален).
POOL_KWARGS: dict = (
    {"poolclass": NullPool}
    if settings.database_url.startswith("postgres")
    else {"pool_pre_ping": True, "pool_recycle": 300}
)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=ENGINE_CONNECT_ARGS,
    **POOL_KWARGS,
)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_models() -> None:
    """Создать таблицы бота (не трогает таблицы метрик)."""
    if settings.database_url.startswith("sqlite"):
        raw_path = settings.database_url.split(":///", 1)[-1]
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await _ensure_schema(conn)
        await conn.run_sync(Base.metadata.create_all)


async def normalize_legacy_roles() -> None:
    """Убрать устаревшую роль manager и зависшие заявки на неё."""
    async with async_session_maker() as session:
        await session.execute(
            update(User)
            .where(User.role == "manager")
            .values(role="user", is_pending_manager=False)
        )
        await session.execute(
            update(User)
            .where(User.is_pending_manager.is_(True))
            .values(is_pending_manager=False)
        )
        await session.commit()


async def sync_admin_ids() -> None:
    """Гарантировать роль admin для всех ID из ADMIN_IDS."""
    admin_ids = settings.admin_id_set
    if not admin_ids:
        return
    async with async_session_maker() as session:
        for telegram_id in admin_ids:
            user = await get_user(session, telegram_id)
            if user is None:
                session.add(User(telegram_id=telegram_id, role="admin"))
            else:
                user.role = "admin"
                user.is_pending_manager = False
        await session.commit()


async def init_async_db() -> None:
    await init_models()
    await normalize_legacy_roles()
    await sync_admin_ids()


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> User:
    """Найти пользователя или создать его с ролью user."""
    user = await get_user(session, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, username=username, full_name=full_name, role="user")
        session.add(user)
    else:
        if username is not None:
            user.username = username
        if full_name is not None:
            user.full_name = full_name
        if user.role == "manager":
            user.role = "user"
            user.is_pending_manager = False

    if telegram_id in settings.admin_id_set:
        user.role = "admin"
        user.is_pending_manager = False

    await session.flush()
    return user


async def create_dialog_entry(
    session: AsyncSession,
    user_id: int,
    question: str,
    answer: str | None = None,
    answered_by: str = "none",
) -> DialogHistory:
    entry = DialogHistory(
        user_id=user_id,
        question=question,
        answer=answer,
        answered_by=answered_by,
        ticket_status="none",
        manager_id=None,
    )
    session.add(entry)
    await session.flush()
    return entry


async def create_jira_session(
    session: AsyncSession,
    user_id: int,
    issue_key: str,
    issue_url: str = "",
) -> JiraSession:
    jira_session = JiraSession(
        user_id=user_id,
        issue_key=issue_key,
        issue_url=issue_url,
        status="open",
        last_comment_id=0,
    )
    session.add(jira_session)
    await session.flush()
    return jira_session


async def get_active_jira_session(session: AsyncSession, user_id: int) -> JiraSession | None:
    result = await session.scalars(
        select(JiraSession)
        .where(JiraSession.user_id == user_id, JiraSession.status == "open")
        .order_by(JiraSession.id.desc())
        .limit(1)
    )
    return result.first()


async def list_active_jira_sessions(session: AsyncSession, limit: int = 100) -> list[JiraSession]:
    result = await session.scalars(
        select(JiraSession)
        .where(JiraSession.status == "open")
        .order_by(JiraSession.id)
        .limit(limit)
    )
    return list(result.all())


async def set_jira_last_comment(
    session: AsyncSession,
    jira_session: JiraSession,
    comment_id: int,
) -> None:
    """Продвинуть watermark, чтобы не отправлять один комментарий дважды."""
    if comment_id > jira_session.last_comment_id:
        jira_session.last_comment_id = int(comment_id)
        await session.flush()


async def close_jira_session(session: AsyncSession, jira_session: JiraSession) -> None:
    jira_session.status = "closed"
    jira_session.closed_at = datetime.utcnow()
    await session.flush()
