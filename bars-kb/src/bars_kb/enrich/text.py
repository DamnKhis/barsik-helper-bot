from __future__ import annotations

import re

from ..config import Settings
from ..providers.base import LLMProvider
from .cache import EnrichmentCache

SYSTEM_TEXT = """Ты — инженер базы знаний. Верни строго JSON без markdown.
Схема: {"summary": "2-4 предложения", "questions": ["3-7 вопросов пользователей"],
"entities": ["термины"], "steps": ["шаги, если это инструкция"]}.
Не добавляй факты, которых нет в тексте. Пиши по-русски."""


def enrich_text(
    settings: Settings,
    llm: LLMProvider | None,
    cache: EnrichmentCache,
    title: str,
    path: list[str],
    text: str,
) -> dict:
    if not text.strip():
        return {"summary": "", "questions": [], "entities": [], "steps": []}
    if llm is None or settings.mock_llm:
        return _fallback(title, text)
    key = cache.key("text", title, text[:4000])
    with cache.lock(key):
        cached = cache.get(key)
        if cached:
            return cached
        user = f"Раздел: {' > '.join(path) or title}\n\nТекст:\n{text[:12000]}"
        result = llm.complete_json(SYSTEM_TEXT, user)
        result = {
            "summary": str(result.get("summary", ""))[:1200],
            "questions": [str(x)[:300] for x in result.get("questions", [])][:8],
            "entities": [str(x)[:120] for x in result.get("entities", [])][:30],
            "steps": [str(x)[:500] for x in result.get("steps", [])][:20],
        }
        cache.set(key, result)
        return result


def _fallback(title: str, text: str) -> dict:
    clean = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    summary = " ".join(sentences[:2])[:600]
    quoted = re.findall(r"«([^»]{2,80})»|\"([^\"]{2,80})\"", clean)
    entities = []
    for pair in quoted:
        value = (pair[0] or pair[1]).strip()
        if value and value not in entities:
            entities.append(value)
    return {
        "summary": summary,
        "questions": [f"Что описано в разделе «{title}»?"],
        "entities": entities[:20],
        "steps": [],
    }
