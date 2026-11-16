"""Разметка решённого обращения для поиска.

Документация и обращения ищутся по-разному. У документации в индекс идёт текст
раздела, и промпт ``enrich_text`` пишет по нему summary/вопросы. У обращения же
полезен **вопрос пользователя**: люди спрашивают так же, как спрашивал автор
тикета, а ответ — это то, что потом уйдёт в генерацию.

Поэтому здесь отдельный промпт: он возвращает перефразировки вопроса (3-5
формулировок), краткую суть решения и термины. Именно они попадают в
``enriched_text``, по которому считается dense-вектор и BM25.
"""

from __future__ import annotations

from ..config import Settings
from ..providers.base import LLMProvider
from .cache import EnrichmentCache

SYSTEM_SUPPORT = """Ты размечаешь РЕШЁННОЕ обращение в поддержку системы «БАРС-Офис».
Верни строго JSON без markdown по схеме:
{
  "summary": "1-2 предложения: в чём была проблема и как её решили",
  "questions": ["3-5 формулировок, которыми другой пользователь мог бы задать этот же вопрос"],
  "answer_digest": "1-3 предложения: что именно нужно сделать (по шагам, если есть шаги)",
  "entities": ["названия разделов, кнопок, документов, ролей"]
}
Только то, что есть во входе: не выдумывай факты и не описывай сам факт переписки.
Пиши по-русски, кратко."""


def enrich_support(
    settings: Settings,
    llm: LLMProvider | None,
    cache: EnrichmentCache,
    question: str,
    answer: str,
    doc_title: str = "",
) -> dict:
    question = question.strip()
    answer = answer.strip()
    if not question and not answer:
        return {"summary": "", "questions": [], "answer_digest": "", "entities": []}
    if llm is None or settings.mock_llm:
        return _fallback(question, answer, doc_title)

    key = cache.key("support", question, answer[:2000])
    with cache.lock(key):
        cached = cache.get(key)
        if cached:
            return cached
        user = (
            f"Обращение: {doc_title}\n\n"
            f"Вопрос пользователя:\n{question[:4000]}\n\n"
            f"Ответ поддержки:\n{answer[:8000]}"
        )
        result = llm.complete_json(SYSTEM_SUPPORT, user)
        normalized = {
            "summary": str(result.get("summary", ""))[:800],
            "questions": [str(x)[:300] for x in result.get("questions", [])][:6],
            "answer_digest": str(result.get("answer_digest", ""))[:1200],
            "entities": [str(x)[:120] for x in result.get("entities", [])][:30],
        }
        cache.set(key, normalized)
        return normalized


def _fallback(question: str, answer: str, doc_title: str) -> dict:
    """Без LLM: вопрос берём как есть, ответ — первой фразой."""
    clean_answer = " ".join(answer.split())
    summary = clean_answer[:300]
    questions = [question] if question else []
    if question and len(question) < 40:
        questions.append(f"{question} в БАРС-Офисе")
    return {
        "summary": summary,
        "questions": questions[:6],
        "answer_digest": clean_answer[:300],
        "entities": [],
    }
