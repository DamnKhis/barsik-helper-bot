"""Общая структура блоков и диспетчер форматов. Роль 1.

Зачем блоки, а не «весь текст сразу»: чанк собирается из блоков одного
раздела (см. chunking.py), таблица не отрывается от заголовка, а скриншот
превращается в подпись через VLM — без этого бот не ответит на вопросы вида
«как выглядит кнопка» (критерий №1 хакатона).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


@dataclass
class Block:
    type: str  # "heading" | "paragraph" | "table"
    text: str
    level: int = 0  # уровень заголовка (только для heading)


def parse_markdown(text: str) -> list[Block]:
    """Минимальный парсер .md — для базы знаний, живущей в Git-репозитории."""
    blocks: list[Block] = []
    table_buf: list[str] = []

    def flush_table() -> None:
        nonlocal table_buf
        if table_buf:
            blocks.append(Block("table", "\n".join(table_buf)))
            table_buf = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            table_buf.append(stripped)
            continue
        flush_table()
        if not stripped:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if match:
            blocks.append(Block("heading", match.group(2).strip(), level=len(match.group(1))))
        else:
            blocks.append(Block("paragraph", stripped))
    flush_table()
    return blocks


def parse_file(path: Path, vlm_caption=None, screenshots_dir: Path | None = None) -> list[Block]:
    """Единая точка входа: выбирает парсер по расширению (ленивые импорты).

    vlm_caption / screenshots_dir использует только PDF-парсер (подписи
    скриншотов); остальные форматы их игнорируют.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from indexer.parse_pdf import parse_pdf  # тяжёлый импорт — только когда нужен

        return parse_pdf(path, vlm_caption=vlm_caption, screenshots_dir=screenshots_dir)
    if suffix == ".docx":
        from indexer.parse_docx import parse_docx

        return parse_docx(path)
    if suffix in {".md", ".txt"}:
        return parse_markdown(path.read_text(encoding="utf-8"))
    raise ValueError(f"Неподдерживаемый формат: {suffix}")
