"""Уверенность и решение об эскалации. Роль 2.

MVP: уверенность = скор лучшего фрагмента из поиска (COSINE ~ 0..1).
Эскалируем, если скор ниже порога или модель ответила фразой «не нашёл».
TODO(роль 2): откалибровать порог на qa/test_cases.csv; варианты точнее —
самооценка LLM (1–10) или логрегрессия на фичах (скор, длина вопроса, тема).
"""
from __future__ import annotations

from app.rag.retriever import RetrievedChunk
from settings import Settings


def estimate_confidence(chunks: list[RetrievedChunk]) -> float:
    if not chunks:
        return 0.0
    return max(0.0, min(1.0, chunks[0].score))


def should_escalate(answer: str, confidence: float, settings: Settings) -> bool:
    if confidence < settings.confidence_threshold:
        return True
    # фраза из промпта генератора = честное «не знаю» -> предлагаем оператора
    return "не нашёл точного ответа" in answer.lower()
