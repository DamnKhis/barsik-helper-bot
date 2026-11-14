from __future__ import annotations

from pathlib import Path

from ..models import Block, Document
from .docx_parser import _slug, build_sections


def parse_text(path: Path, images_dir: Path, version: str) -> Document:
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks: list[Block] = []
    for paragraph in text.split("\n\n"):
        value = " ".join(paragraph.split())
        if value:
            blocks.append(Block(kind="paragraph", text=value))
    title = path.stem
    return Document(
        doc_id=_slug(title),
        title=title,
        source_path=str(path),
        source_type="text",
        version=version,
        sections=build_sections(blocks, fallback_title=title),
        page_count=0,
    )
