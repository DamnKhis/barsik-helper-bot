"""Мост между ботом и RAG-ядром.

RAG-модуль (app/rag/retriever.py) принадлежит другой команде, поэтому импорт
ленивый: если зависимости/Qdrant недоступны, бот продолжает работать на
заглушке из ai_stub.py.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.bot.services.ai_stub import get_ai_answer

logger = logging.getLogger(__name__)

MAX_TG_TEXT_LEN = 3950


def _clamp_tg_text(text: str) -> str:
    if len(text) <= MAX_TG_TEXT_LEN:
        return text
    cut = text[:MAX_TG_TEXT_LEN].rsplit("\n", 1)[0].rstrip()
    return f"{cut}\n…\n(текст сокращён под лимит Telegram)"


@dataclass(slots=True)
class AIResult:
    answer: str
    flag: str = "ANSWER"
    need_operator: bool = False
    source: str | None = None
    known: bool = True
    confidence: float = 0.0
    images: list[str] = field(default_factory=list)


class AIService:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._retriever: Any | None = None
        self._init_attempted = False
        self._lock = asyncio.Lock()

    @property
    def using_real_ai(self) -> bool:
        return self._retriever is not None

    async def _ensure_retriever(self) -> Any | None:
        if not self.enabled or self._init_attempted:
            return self._retriever

        async with self._lock:
            if self._init_attempted:
                return self._retriever
            self._init_attempted = True
            try:
                from app.rag.retriever import Retriever

                self._retriever = await asyncio.to_thread(Retriever)
                logger.info("RAG-ядро подключено к боту")
            except Exception as exc:
                logger.warning("RAG-ядро недоступно, используется заглушка ИИ: %s", exc)
                self._retriever = None
            return self._retriever

    async def answer(
        self,
        user_id: int,
        question: str,
        context: list[dict],
        bot_username: str | None = None,
    ) -> AIResult:
        retriever = await self._ensure_retriever()
        if retriever is not None:
            try:
                result = await retriever.answer_query(question, bot_username=bot_username)
                flag = str(getattr(result, "flag", "ANSWER"))
                text = _clamp_tg_text(str(getattr(result, "answer", "") or "").strip())
                if not text:
                    raise ValueError("RAG вернул пустой ответ")
                return AIResult(
                    answer=text,
                    flag=flag,
                    need_operator=bool(getattr(result, "need_operator", False)) or flag == "ESCALATE",
                    source=getattr(result, "source", None),
                    known=flag == "ANSWER",
                    confidence=float(getattr(result, "confidence", 0.0) or 0.0),
                    images=list(getattr(result, "images", []) or []),
                )
            except Exception as exc:
                logger.warning("Ошибка RAG-ядра, fallback на заглушку: %s", exc)

        known, text = await get_ai_answer(user_id, question, context)
        return AIResult(
            answer=_clamp_tg_text(text),
            flag="ANSWER" if known else "ESCALATE",
            need_operator=not known,
            known=known,
            confidence=1.0 if known else 0.0,
        )
