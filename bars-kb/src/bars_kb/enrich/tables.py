from __future__ import annotations

from ..config import Settings
from ..models import table_to_markdown
from ..providers.base import LLMProvider
from .cache import EnrichmentCache


SYSTEM_TABLE = """Ты — инженер базы знаний. Верни строго JSON без markdown.
Схема: {"description": "что показывает таблица и как её читать",
"questions": ["вопросы, на которые отвечает таблица"], "entities": ["термины"]}.
Не добавляй факты, которых нет в таблице. Пиши по-русски."""


def enrich_table(
    settings: Settings,
    llm: LLMProvider | None,
    cache: EnrichmentCache,
    title: str,
    rows: list[list[str]],
) -> dict:
    markdown = table_to_markdown(rows)
    if not rows:
        return {"description": "", "questions": [], "entities": []}
    if llm is None or settings.mock_llm:
        header = " | ".join(rows[0]) if rows else ""
        return {
            "description": f"Таблица «{title}»: {header}. Строк: {max(0, len(rows) - 1)}.",
            "questions": [f"Что показано в таблице «{title}»?"],
            "entities": [c for c in rows[0] if c][:10],
        }
    key = cache.key("table", title, markdown[:4000])
    cached = cache.get(key)
    if cached:
        return cached
    result = llm.complete_json(SYSTEM_TABLE, f"Таблица: {title}\n\n{markdown[:12000]}")
    result = {
        "description": str(result.get("description", ""))[:1200],
        "questions": [str(x)[:300] for x in result.get("questions", [])][:8],
        "entities": [str(x)[:120] for x in result.get("entities", [])][:30],
    }
    cache.set(key, result)
    return result
