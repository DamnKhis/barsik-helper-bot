"""Парсинг .docx и .md в плоский список блоков. Роль 1.

Зачем блоки, а не «весь текст сразу»: чанк собирается из блоков одного
раздела (см. chunking.py), таблица не отрывается от заголовка, а скриншот
превращается в подпись через VLM — без этого бот не ответит на вопросы вида
«как выглядит кнопка» (критерий №1 хакатона).

TODO(роль 1):
  - скриншоты: достать изображения из docx (doc.part.related_parts) и получить
    подпись через VLM (OpenAI-совместимый chat/completions с image_url);
    подпись добавлять блоком-абзацем сразу после картинки;
  - стиль «List Paragraph» -> маркированный список («- ») для лучшей нарезки;
  - объединённые ячейки таблиц дублируются текстом — при желании дедуплицировать.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.document import Document as _Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

HEADING_RE = re.compile(r"heading\s*(\d+)", re.IGNORECASE)


@dataclass
class Block:
    type: str  # "heading" | "paragraph" | "table"
    text: str
    level: int = 0  # уровень заголовка (только для heading)


def _iter_block_items(doc: _Document):
    """Абзацы и таблицы в исходном порядке следования."""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _table_to_markdown(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) or " " for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if len(rows) >= 2:
        cols = rows[0].count("|") - 1
        rows.insert(1, "|" + "|".join([" --- "] * cols) + "|")
    return "\n".join(rows)


def parse_docx(path: Path) -> list[Block]:
    doc = Document(str(path))
    blocks: list[Block] = []
    for item in _iter_block_items(doc):
        if isinstance(item, Paragraph):
            text = " ".join(item.text.split())
            if not text:
                continue
            style = item.style.name if item.style is not None else ""
            match = HEADING_RE.match(style)
            if match:
                blocks.append(Block("heading", text, level=int(match.group(1))))
            else:
                blocks.append(Block("paragraph", text))
        elif isinstance(item, Table):
            blocks.append(Block("table", _table_to_markdown(item)))
    return blocks


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


def parse_file(path: Path) -> list[Block]:
    if path.suffix.lower() == ".docx":
        return parse_docx(path)
    if path.suffix.lower() in {".md", ".txt"}:
        return parse_markdown(path.read_text(encoding="utf-8"))
    raise ValueError(f"Неподдерживаемый формат: {path.suffix}")
