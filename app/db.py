"""SQLite-слой данных RAG-бота «БАРС-Офис»: лог запросов + лог реиндексаций.

Итоговая схема (договорились: без колонки intent, гейт-классификатор не делаем;
нерелевантные вопросы видны как outcome='not_found' — отдельная метрика не нужна).

Кто что делает:
  - бот после каждого ответа вызывает log_request(...), на кнопки 👍/👎 — set_rating(...);
  - эндпоинт GET /stats читает таблицу requests своими SQL-агрегациями;
  - git-интеграция (webhook/poller) пишет реиндексации через log_reindex(...).

Путь к файлу БД задаётся переменной окружения DB_PATH (добавить в .env.example).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

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
