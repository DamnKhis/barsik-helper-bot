"""Smoke-тест: без внешних сервисов /ask отвечает честным «не знаю» + эскалацией.

Это ключевое поведение брифа: бот НЕ галлюцинирует, даже когда база пуста.
"""
from fastapi.testclient import TestClient

from app.main import create_app
from settings import get_settings


def test_ask_honest_fallback_without_services(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # чтобы не трогать ./data
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()  # настройки кэшируются — сбрасываем под этот тест

    with TestClient(create_app()) as client:
        response = client.post(
            "/ask",
            json={"text": "Как оформить командировку?", "user_id": "42"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["confidence"] == 0.0
    assert data["sources"] == []
    assert data["escalate"] is True
    assert "не нашёл" in data["answer"]


def test_stats_endpoint_returns_all_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        client.post("/ask", json={"text": "Где настроить фильтр?", "user_id": "1"})
        stats = client.get("/stats").json()

    assert stats["total"] == 1
    assert stats["today"] == 1
    assert set(stats) >= {"auto_answer_rate", "escalation_rate", "by_hour", "top_topics", "blind_spots"}
