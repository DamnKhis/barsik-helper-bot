from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    kind: str  # heading | paragraph | list | table | image
    text: str = ""
    level: int = 0
    page: int = 0
    table: list[list[str]] | None = None
    image_path: str = ""
    caption: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Section:
    title: str
    level: int
    path: list[str]
    page: int = 0
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.kind in {"paragraph", "list"} and block.text.strip():
                parts.append(block.text.strip())
            elif block.kind == "table" and block.table:
                parts.append(table_to_markdown(block.table))
        return "\n\n".join(parts).strip()

    @property
    def images(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "image"]

    @property
    def tables(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "table"]


@dataclass
class Document:
    doc_id: str
    title: str
    source_path: str
    source_type: str
    version: str
    sections: list[Section] = field(default_factory=list)
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.sections if s.text)

    @property
    def section_count(self) -> int:
        return len(self.sections)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    section_path: list[str]
    page: int
    chunk_type: str
    raw_text: str
    enriched_text: str
    summary: str
    parent_id: str
    parent_text: str
    source_url: str
    image_path: str = ""
    image_url: str = ""
    table_json: dict[str, Any] | None = None
    diagram_mermaid: str = ""
    entities: list[str] = field(default_factory=list)
    content_hash: str = ""
    version: str = ""
    pipeline_version: str = "0.1.0"
    meta: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "section_path": self.section_path,
            "page": self.page,
            "chunk_type": self.chunk_type,
            "raw_text": self.raw_text,
            "enriched_text": self.enriched_text,
            "summary": self.summary,
            "parent_id": self.parent_id,
            "parent_text": self.parent_text,
            "source_url": self.source_url,
            "image_path": self.image_path,
            "image_url": self.image_url,
            "table_json": self.table_json,
            "diagram_mermaid": self.diagram_mermaid,
            "entities": self.entities,
            "content_hash": self.content_hash,
            "version": self.version,
            "pipeline_version": self.pipeline_version,
            **self.meta,
        }


def table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    header = norm[0]
    lines = ["| " + " | ".join(_cell(c) for c in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in norm[1:]:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return "\n".join(lines)


def _cell(value: str) -> str:
    return " ".join(str(value).replace("|", "\\|").split())
