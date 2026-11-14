"""Синтетическая нагрузка для дашборда перед демо. Роль 4.

Пишет в ту же SQLite-базу, которую заполняет API (data/analytics.db),
распределяя запросы за последние 24 часа — графики «оживают» до прихода жюри.
Зависимостей не требует (только stdlib).

Запуск: python scripts/seed_stats.py --n 120
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "analytics.db"

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
CREATE TABLE IF NOT EXISTS reindex_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    source        TEXT,
    files_changed INTEGER,
    status        TEXT,
    details       TEXT
);
"""

# (вопрос, тема, уверенный ли ответ)
SAMPLES = [
    ("Как оформить командировку?", "Командировки", True),
    ("Как считается аванс за командировку?", "Командировки", True),
    ("Кто оплачивает билеты в командировке?", "Командировки", True),
    ("Какие статусы бывают у мебели?", "Мебель и рабочие места", True),
    ("Как заказать новое кресло на замену?", "Мебель и рабочие места", True),
    ("Что значит статус «списан» у мебели?", "Мебель и рабочие места", True),
    ("Какие операторы сравнения есть в фильтрах?", "БАРС-Офис: интерфейс", True),
    ("Почему сделка не выбирается в фильтре?", "БАРС-Офис: интерфейс", True),
    ("Что за ссылка bo.bars.group?", "БАРС-Офис: интерфейс", True),
    ("Как выглядит панель быстрого доступа?", "БАРС-Офис: интерфейс", False),
    ("Где настроить производственный календарь?", "БАРС-Офис: интерфейс", True),
    ("Кто выиграл вчера матч?", "Другое", False),
    ("Как заказать пропуск на парковку?", "Другое", False),
    ("Позови оператора", "Эскалация", False),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Демо-данные для дашборда")
    parser.add_argument("--n", type=int, default=120, help="сколько запросов сгенерировать")
    args = parser.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    random.seed(42)
    now = datetime.now()

    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM query_log")  # повторный seed не накапливает мусор

        for i in range(args.n):
            question, topic, confident = random.choice(SAMPLES)
            minutes_ago = random.randint(0, 24 * 60)
            ts = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
            if confident:
                confidence = random.uniform(0.55, 0.92)
                escalated = 0
            else:
                confidence = random.uniform(0.05, 0.35)
                escalated = 1
            conn.execute(
                "INSERT INTO query_log (ts, user_id, question, topic, confidence, auto_answered, escalated, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    str(random.randint(100, 999)),
                    question,
                    topic,
                    round(confidence, 2),
                    0 if escalated else 1,
                    escalated,
                    random.randint(700, 3200),
                ),
            )
        conn.execute(
            "INSERT INTO reindex_log (ts, source, files_changed, status, details) "
            "VALUES (?, 'manual', 3, 'ok', 'indexed=3 errors=0')",
            ((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.commit()

    print(f"OK: {args.n} записей в {DB_PATH}")
    print("Откройте дашборд: http://localhost:8000/dashboard")


if __name__ == "__main__":
    main()
