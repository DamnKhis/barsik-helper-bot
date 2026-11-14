"""Заглушка ИИ.

Часть с RAG/ИИ пишут другие люди — здесь только стабильный контракт.
Позже функцию заменят реальным вызовом RAG-ядра.
"""

from __future__ import annotations


async def get_ai_answer(
    user_id: int,
    question: str,
    context: list[dict],
) -> tuple[bool, str]:
    """Вернуть ``(known_answer, response_text)``.

    Для тестирования тикетов: фразы «не знаю» или «сложный вопрос»
    заставляют бота эскалировать вопрос менеджеру.
    """
    del user_id, context
    text = (question or "").lower()
    if "не знаю" in text or "сложный вопрос" in text:
        return False, "Я не знаю ответ на этот вопрос"
    return True, "Ответ ИИ с учетом контекста"


# Удобные алиасы на случай, если соседний модуль вызовет другое имя.
ask_ai = get_ai_answer
ai_answer = get_ai_answer
get_ai_response = get_ai_answer
