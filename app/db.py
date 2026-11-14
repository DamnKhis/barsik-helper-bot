<<<<<<< HEAD
"""SQLite-слой данных RAG-бота «БАРС-Офис»: лог запросов + лог реиндексаций.

Итоговая схема (договорились: без колонки intent, гейт-классификатор не делаем;
нерелевантные вопросы видны как outcome='not_found' — отдельная метрика не нужна).

Кто что делает:
  - бот после каждого ответа вызывает log_request(...), на кнопки 👍/👎 — set_rating(...);
  - эндпоинт GET /stats читает таблицу requests своими SQL-агрегациями;
  - git-интеграция (webhook/poller) пишет реиндексации через log_reindex(...).

Путь к файлу БД задаётся переменной окружения DB_PATH (добавить в .env.example).
=======
"""Слой данных бота «БАРС-Офис».

Здесь живут две независимые части:

1. Логи метрик в SQLite (таблицы ``requests`` / ``reindex_log``): их пишет бот,
   читает дашборд. Расчёт метрик и эндпоинт /stats — зона ответственности
   отдельной команды, эти функции менять не нужно.
2. Пользователи и тикеты (``users`` / ``dialog_history``) на SQLAlchemy async.
   Это данные бота и менеджеров.

Подключение к SQLAlchemy-части задаётся DATABASE_URL.
Путь к SQLite-логу метрик задаётся DB_PATH.
>>>>>>> origin/tg-bot
"""
from __future__ import annotations

import logging
import os
import sqlite3
<<<<<<< HEAD
from pathlib import Path

=======
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings

>>>>>>> origin/tg-bot
logger = logging.getLogger(__name__)

OUTCOMES = ("answered", "not_found", "escalated")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    user_id        TEXT    NOT NULL,
    question       TEXT    NOT NULL,
    confidence     REAL    NOT NULL DEFAULT 0,
    outcome        TEXT    NOT NULL CHECK (outcome IN ('answered', 'not_found', 'escalated')),
    response_ms    INTEGER NOT NULL DEFAULT 0,
    docs           TEXT    NOT NULL DEFAULT '',
    rating         INTEGER,
    request_msg_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts  ON requests (ts);
CREATE INDEX IF NOT EXISTS idx_requests_out ON requests (outcome);
CREATE INDEX IF NOT EXISTS idx_requests_msg ON requests (user_id, request_msg_id);

CREATE TABLE IF NOT EXISTS reindex_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    source        TEXT,
    files_changed INTEGER,
    status        TEXT,
    details       TEXT
);
"""


def _run(db_path: str, sql: str, params: tuple) -> sqlite3.Cursor:
    """Один запрос с гарантированным закрытием соединения и таймаутом на блокировку."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        with conn:
            return conn.execute(sql, params)
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Создать каталог и таблицы; включить WAL (бот и API пишут из разных процессов)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def log_request(
    db_path: str,
    user_id: str,
    question: str,
    confidence: float,
    outcome: str,
    response_ms: int = 0,
    docs: str = "",
    msg_id: int | None = None,
) -> None:
    """Записать один вопрос-ответ. Вызывает бот сразу после отправки ответа.

    outcome:  'answered' | 'not_found' | 'escalated'
    docs:     источники через '; ' — "Руководство HR.pdf, стр. 14; Приказ 27.pdf"
    msg_id:   id сообщения бота с ответом — по нему позже привяжется 👍/👎
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome должен быть одним из {OUTCOMES}, получено: {outcome!r}")
    try:
        _run(
            db_path,
            "INSERT INTO requests (user_id, question, confidence, outcome, response_ms, docs, request_msg_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(user_id), question, float(confidence), outcome, int(response_ms), docs, msg_id),
        )
    except sqlite3.Error as exc:
        logger.error("Не удалось записать запрос в БД: %s", exc)


def set_rating(db_path: str, user_id: str, msg_id: int, rating: int) -> bool:
    """Привязать 👍 (1) / 👎 (-1) к ответу бота. Вызывает бот из обработчика кнопки.

    Ищем по паре (user_id, msg_id): в Telegram id сообщений уникальны внутри чата,
    поэтому пара однозначно определяет строку. Возвращает True, если строка нашлась.
    """
    if rating not in (1, -1):
        raise ValueError(f"rating должен быть 1 или -1, получено: {rating!r}")
    try:
        cur = _run(
            db_path,
            "UPDATE requests SET rating = ? WHERE user_id = ? AND request_msg_id = ?",
            (rating, str(user_id), int(msg_id)),
        )
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        logger.error("Не удалось сохранить оценку: %s", exc)
        return False


def log_reindex(db_path: str, source: str, files_changed: int, status: str, details: str = "") -> None:
    """Лог реиндексаций для git-интеграции: какой webhook/poller и сколько файлов обновил."""
    try:
        _run(
            db_path,
            "INSERT INTO reindex_log (source, files_changed, status, details) VALUES (?, ?, ?, ?)",
            (source, int(files_changed), status, details),
        )
    except sqlite3.Error as exc:
        logger.error("Не удалось записать лог реиндексации: %s", exc)


if __name__ == "__main__":
    # Смоук-тест: python3 -I db.py — создаёт временную БД, проверяет запись/рейтинг/агрегаты.
    import tempfile

    db = os.path.join(tempfile.mkdtemp(), "test.db")
    init_db(db)
    log_request(db, "42", "Как взять отпуск?", 0.82, "answered", 1730, "Отпуска.pdf, стр. 5", msg_id=101)
    log_request(db, "42", "А где бланк заявления?", 0.64, "answered", 1500, "Отпуска.pdf, стр. 5", msg_id=102)
    log_request(db, "7", "Можно ли фильтровать по !> 200?", 0.18, "not_found", 1900)
    log_request(db, "7", "Перевести на человека", 0.90, "escalated", 2100)
    print("set_rating (есть строка):", set_rating(db, "42", 101, 1))
    print("set_rating (нет строки): ", set_rating(db, "42", 999, -1))

    conn = sqlite3.connect(db)
    n, ok = conn.execute("SELECT COUNT(*), SUM(outcome = 'answered') FROM requests").fetchone()
    print(f"всего={n}, автоответы={ok} ({100 * ok / n:.0f}%)")
    blind = conn.execute(
        "SELECT question FROM requests WHERE confidence < 0.45 ORDER BY ts DESC"
    ).fetchall()
    print("слепые зоны:", [r[0] for r in blind])
    conn.close()
    print("OK: смоук-тест прошёл")
<<<<<<< HEAD
=======


# ---------------------------------------------------------------------------
# SQLAlchemy async: пользователи, роли и тикеты
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
    ticket_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False, index=True)
    manager_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_models() -> None:
    """Создать таблицы users/dialog_history (не трогает таблицы метрик)."""
    if settings.database_url.startswith("sqlite"):
        raw_path = settings.database_url.split(":///", 1)[-1]
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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

    if telegram_id in settings.admin_id_set:
        user.role = "admin"
        user.is_pending_manager = False

    await session.flush()
    return user


async def set_pending_manager(session: AsyncSession, telegram_id: int) -> bool:
    """Пометить заявку пользователя на права менеджера."""
    result = await session.execute(
        update(User)
        .where(User.telegram_id == telegram_id, User.role == "user")
        .values(is_pending_manager=True)
    )
    return result.rowcount > 0


async def list_pending_managers(session: AsyncSession) -> list[User]:
    result = await session.scalars(
        select(User).where(User.is_pending_manager.is_(True)).order_by(User.created_at)
    )
    return list(result.all())


async def approve_manager(session: AsyncSession, telegram_id: int) -> User | None:
    """Выдать пользователю права менеджера и снять флаг заявки."""
    user = await get_user(session, telegram_id)
    if user is None:
        return None
    user.role = "manager"
    user.is_pending_manager = False
    await session.flush()
    return user


async def list_by_role(session: AsyncSession, role: str) -> list[User]:
    result = await session.scalars(select(User).where(User.role == role).order_by(User.created_at))
    return list(result.all())


async def create_dialog_entry(
    session: AsyncSession,
    user_id: int,
    question: str,
    answer: str | None = None,
    answered_by: str = "none",
    ticket_status: str = "none",
    manager_id: int | None = None,
) -> DialogHistory:
    entry = DialogHistory(
        user_id=user_id,
        question=question,
        answer=answer,
        answered_by=answered_by,
        ticket_status=ticket_status,
        manager_id=manager_id,
    )
    session.add(entry)
    await session.flush()
    return entry


async def get_ticket(session: AsyncSession, ticket_id: int) -> DialogHistory | None:
    return await session.get(DialogHistory, ticket_id)


async def list_open_tickets(session: AsyncSession, limit: int = 50) -> list[DialogHistory]:
    result = await session.scalars(
        select(DialogHistory)
        .where(DialogHistory.ticket_status == "open")
        .order_by(DialogHistory.created_at)
        .limit(limit)
    )
    return list(result.all())


async def take_ticket(session: AsyncSession, ticket_id: int, manager_id: int) -> bool:
    """Атомарно перевести открытый тикет в работу."""
    result = await session.execute(
        update(DialogHistory)
        .where(DialogHistory.id == ticket_id, DialogHistory.ticket_status == "open")
        .values(ticket_status="in_progress", manager_id=manager_id)
    )
    return result.rowcount > 0


async def close_ticket(
    session: AsyncSession,
    ticket_id: int,
    manager_id: int,
    answer: str,
) -> bool:
    """Закрыть тикет ответом менеджера."""
    result = await session.execute(
        update(DialogHistory)
        .where(
            DialogHistory.id == ticket_id,
            DialogHistory.ticket_status == "in_progress",
            DialogHistory.manager_id == manager_id,
        )
        .values(answer=answer, answered_by="manager", ticket_status="closed")
    )
    return result.rowcount > 0
>>>>>>> origin/tg-bot
