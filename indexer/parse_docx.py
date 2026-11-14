"""Парсинг .docx в блоки (запасной парсер). Роль 1.

Основной формат команды — PDF (см. parse_pdf.py, PyMuPDF). Этот парсер
оставлен как запасной: если пришлют .docx, пайплайн работает без изменений.
Общий тип Block и диспетчер parse_file живут в indexer/blocks.py.

TODO(роль 1):
  - скриншоты из docx (doc.part.related_parts) -> подпись через VLM,
    как это уже сделано в parse_pdf.make_vlm_captioner();
  - стиль «List Paragraph» -> маркированный список («- ») для лучшей нарезки;
  - объединённые ячейки таблиц дублируются текстом — дедуплицировать.
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.document import Document as _Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from indexer.blocks import Block

HEADING_RE = re.compile(r"heading\s*(\d+)", re.IGNORECASE)

__all__ = ["Block", "parse_docx"]


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
