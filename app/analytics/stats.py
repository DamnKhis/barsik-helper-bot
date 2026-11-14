"""Агрегаты для дашборда. Роль 4.

Метрики из брифа: количество запросов, темы, % успешных автоответов.
Плюс «слепые зоны базы знаний» (п.5 будущих возможностей): вопросы, на которых
бот был неуверен или передал оператору — это рабочий инструмент для улучшения
документации.
"""
from __future__ import annotations

import sqlite3

from settings import Settings


def get_stats(settings: Settings) -> dict:
    with sqlite3.connect(settings.db_path) as conn:
        cur = conn.cursor()

        total = cur.execute("SELECT COUNT(*) FROM query_log").fetchone()[0]
        today = cur.execute(
            "SELECT COUNT(*) FROM query_log WHERE date(ts) = date('now', 'localtime')"
        ).fetchone()[0]
        auto = cur.execute("SELECT AVG(auto_answered) FROM query_log").fetchone()[0] or 0
        escalations = cur.execute("SELECT AVG(escalated) FROM query_log").fetchone()[0] or 0
        confidence = cur.execute(
            "SELECT AVG(confidence) FROM query_log WHERE confidence IS NOT NULL"
        ).fetchone()[0] or 0
        latency = cur.execute(
            "SELECT AVG(latency_ms) FROM query_log WHERE latency_ms IS NOT NULL"
        ).fetchone()[0] or 0

        by_hour = {
            row[0]: row[1]
            for row in cur.execute(
                "SELECT strftime('%H', ts), COUNT(*) FROM query_log "
                "WHERE ts >= datetime('now', 'localtime', '-24 hours') GROUP BY 1 ORDER BY 1"
            )
        }
        top_topics = [
            {"topic": row[0], "count": row[1]}
            for row in cur.execute(
                "SELECT COALESCE(topic, 'Другое'), COUNT(*) FROM query_log GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
            )
        ]
        blind_spots = [
            {"question": row[0], "ts": row[1], "confidence": row[2]}
            for row in cur.execute(
                "SELECT question, ts, confidence FROM query_log "
                "WHERE escalated = 1 OR confidence < ? ORDER BY id DESC LIMIT 10",
                (settings.confidence_threshold,),
            )
        ]
        reindex = [
            {"ts": row[0], "source": row[1], "files": row[2], "status": row[3]}
            for row in cur.execute(
                "SELECT ts, source, files_changed, status FROM reindex_log ORDER BY id DESC LIMIT 5"
            )
        ]

    return {
        "total": total,
        "today": today,
        "auto_answer_rate": round(auto * 100, 1),
        "escalation_rate": round(escalations * 100, 1),
        "avg_confidence": round(confidence, 2),
        "avg_latency_ms": int(latency),
        "by_hour": by_hour,
        "top_topics": top_topics,
        "blind_spots": blind_spots,
        "reindex_log": reindex,
        "confidence_threshold": settings.confidence_threshold,
    }
