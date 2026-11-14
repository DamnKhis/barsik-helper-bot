from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, Document
from .docx_parser import _slug, build_sections


def parse_markdown(path: Path, images_dir: Path, version: str) -> Document:
    text = path.read_text(encoding="utf-8")
    blocks: list[Block] = []
    paragraph: list[str] = []
    table: list[list[str]] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(Block(kind="paragraph", text=" ".join(paragraph).strip()))
            paragraph.clear()

    def flush_table() -> None:
        if len(table) >= 2:
            blocks.append(Block(kind="table", table=[row[:] for row in table]))
        table.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            flush_table()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            flush_table()
            blocks.append(Block(kind="heading", text=heading.group(2).strip(), level=len(heading.group(1))))
            continue
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            flush_paragraph()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                table.append(cells)
            continue
        image = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
        if image:
            flush_paragraph()
            blocks.append(
                Block(
                    kind="image",
                    caption=image.group(1),
                    image_path=image.group(2),
                    meta={"source": "markdown"},
                )
            )
            continue
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line):
            flush_paragraph()
            blocks.append(Block(kind="list", text=line.strip()))
            continue
        paragraph.append(line.strip())
    flush_paragraph()
    flush_table()

    title = next((b.text for b in blocks if b.kind == "heading"), path.stem)
    return Document(
        doc_id=_slug(path.stem),
        title=title,
        source_path=str(path),
        source_type="markdown",
        version=version,
        sections=build_sections(blocks, fallback_title=title),
        page_count=0,
    )
