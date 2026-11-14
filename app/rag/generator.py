"""Генерация ответа LLM со строгой привязкой к источникам. Роль 2.

Промпт — главный инструмент против галлюцинаций (критерий №2):
- отвечать только по найденным фрагментам;
- всегда указывать «Источник: …, раздел …»;
- нет данных -> честное «не нашёл» + предложение оператора.
Формулировки можно улучшать, но правило честного «не знаю» не убирать.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from app.rag.retriever import RetrievedChunk
from settings import Settings, get_settings

logger = logging.getLogger(__name__)

NO_ANSWER_PHRASE = "Я пока не нашёл точного ответа в базе знаний. Могу передать ваш вопрос дежурному оператору."

SYSTEM_PROMPT = """Ты — виртуальный помощник первой линии поддержки по продукту «БАРС-Офис».

Правила:
1. Отвечай ТОЛЬКО на основе фрагментов документации, приведённых в сообщении пользователя.
2. В конце ответа обязательно укажи источник в формате: «Источник: <документ>, раздел <раздел>».
3. Если во фрагментах нет ответа на вопрос, ответь ровно одной фразой: «Я пока не нашёл точного ответа в базе знаний. Могу передать ваш вопрос дежурному оператору.»
4. Обращайся к пользователю на «Вы», тон дружелюбный, ответ краткий и по делу.
5. Никогда не выдумывай цифры, названия кнопок, пунктов меню и разделов.
"""


class Generator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = AsyncOpenAI(
            base_url=self.settings.llm_base_url, api_key=self.settings.llm_api_key or "not-set"
        )
        self.model = self.settings.llm_model

    async def generate(self, question: str, chunks: list[RetrievedChunk], history: list[dict]) -> str:
        if not chunks:
            return NO_ANSWER_PHRASE
        context = "\n\n".join(
            f"[Фрагмент {i}] Документ: «{chunk.doc}», раздел: {chunk.section_path}\n{chunk.text}"
            for i, chunk in enumerate(chunks, start=1)
        )
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
        messages.append(
            {
                "role": "user",
                "content": f"Вопрос пользователя: {question}\n\nФрагменты документации:\n{context}",
            }
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0.2
            )
            return (response.choices[0].message.content or NO_ANSWER_PHRASE).strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка LLM: %s", exc)
            return (
                "Произошла техническая ошибка при обращении к языковой модели. "
                "Попробуйте ещё раз чуть позже или позовите оператора."
            )
