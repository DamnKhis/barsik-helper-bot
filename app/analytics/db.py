"""SQLite-логи запросов и реиндексаций. Роль 4.

SQLite достаточно для MVP: одна машина, дашборд читает тот же файл.
Тема запроса: MVP-эвристика по ключевым словам; TODO(роль 4) — один LLM-вызов
на пачку вопросов раз в N минут (дешевле и точнее правил).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS query_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    user_id       TEXT NOT NULL,
    question      TEXT NOT NULL,
    topic         TEXT,
    confidence    REAL,
    auto_answered INTEGER NOT NULL DEFAULT 1,
    escalated     INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_query_ts ON query_log (ts);
CREATE TABLE IF NOT EXISTS reindex_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    source        TEXT,
    files_changed INTEGER,
    status        TEXT,
    details       TEXT
);
"""

TOPIC_KEYWORDS = {
    "Командировки": ("командировк", "аванс", "суточн", "поездк", "заявка на командировку"),
    "Мебель и рабочие места": ("мебел", "стол", "стул", "кресл", "шкаф", "рабочее место", "списан"),
    "БАРС-Офис: интерфейс": ("кнопк", "фильтр", "воронк", "интерфейс", "настро", "сделк", "bo.bars.group", "календар"),
    "Эскалация": ("оператор", "человек", "специалист"),
}


def guess_topic(question: str) -> str:
    lowered = question.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return topic
    return "Другое"


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def log_query(
    db_path: str,
    user_id: str,
    question: str,
    topic: str,
    confidence: float,
    auto_answered: bool,
    escalated: bool,
    latency_ms: int,
) -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO query_log (user_id, question, topic, confidence, auto_answered, escalated, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, question, topic, confidence, int(auto_answered), int(escalated), latency_ms),
            )
    except sqlite3.Error as exc:
        logger.error("Не удалось записать лог запроса: %s", exc)


def log_reindex(db_path: str, source: str, files_changed: int, status: str, details: str = "") -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO reindex_log (source, files_changed, status, details) VALUES (?, ?, ?, ?)",
                (source, files_changed, status, details),
            )
    except sqlite3.Error as exc:
        logger.error("Не удалось записать лог реиндексации: %s", exc)
