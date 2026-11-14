"""Контракт API — единый для бота и RAG-ядра. Менять только всем вместе (AGENTS.md, п.2)."""
from typing import Literal

from pydantic import BaseModel, Field


class Source(BaseModel):
    doc: str = Field(description="Имя документа-источника")
    section: str = Field(default="", description="Путь раздела, напр. «2.3 Расчёт аванса»")
    link: str | None = None
    score: float | None = Field(default=None, description="Релевантность из поиска (0..1)")


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AskRequest(BaseModel):
    text: str = Field(min_length=1, description="Вопрос пользователя")
    user_id: str | int
    history: list[HistoryMessage] = Field(
        default_factory=list, description="Последние сообщения диалога (контекст)"
    )


class AskResponse(BaseModel):
    answer: str
    sources: list[Source] = []
    confidence: float = Field(ge=0, le=1)
    escalate: bool = Field(description="True -> бот предлагает оператора")
