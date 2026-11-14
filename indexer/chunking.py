"""Чанкинг блоков по разделам с сохранением пути раздела. Роль 1.

Почему так:
- вопросы «где настроить X» ищут именно раздел — путь раздела в метаданных и в
  шапке чанка резко поднимает точность (критерий №1);
- шапка «Документ: … | Раздел: …» попадает в промпт LLM, поэтому модель умеет
  отвечать в требуемом брифом формате «Источник: …, раздел 2.3»;
- длинный раздел режется по абзацам, но каждый кусок начинается с шапки.
"""
from __future__ import annotations

from dataclasses import dataclass

from indexer.blocks import Block

MAX_CHUNK_CHARS = 1500  # ~350-400 токенов: крупнее — размывается поиск, мельче — теряется контекст


@dataclass
class Chunk:
    doc: str
    section_path: str
    text: str  # готовый текст для эмбеддинга (с шапкой)
    link: str | None = None

    def payload(self) -> dict:
        return {
            "text": self.text,
            "doc": self.doc,
            "section_path": self.section_path,
            "link": self.link or "",
        }


def _section_title(stack: list[tuple[int, str]]) -> str:
    return " / ".join(title for _, title in stack) if stack else "Общие положения"


def _split_long(body: str) -> list[str]:
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for para in body.split("\n"):
        if size + len(para) > MAX_CHUNK_CHARS and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def chunk_document(doc_name: str, blocks: list[Block], link_prefix: str | None = None) -> list[Chunk]:
    """Собирает чанки: буфер текста закрывается при каждом новом заголовке."""
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        section = _section_title(stack)
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body:
            return
        header = f"Документ: {doc_name} | Раздел: {section}"
        for part in _split_long(body):
            chunks.append(Chunk(doc=doc_name, section_path=section, text=f"{header}\n{part}"))

    for block in blocks:
        if block.type == "heading":
            flush()
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
        else:
            buffer.append(block.text)
    flush()

    if link_prefix:
        for i, chunk in enumerate(chunks, start=1):
            chunk.link = f"{link_prefix}#{i}"
    return chunks
