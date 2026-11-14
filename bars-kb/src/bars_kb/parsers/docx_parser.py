from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..models import Block, Document, Section
from .clean import clean_lines, is_toc_page


def _slug(text: str) -> str:
    table = str.maketrans(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "abvgdeejzijklmnoprstufhccss_y_eua",
    )
    value = text.lower().translate(table)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:60] or "document"


def parse_docx(path: Path, images_dir: Path, version: str) -> Document:
    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn

    doc = DocxDocument(str(path))
    images_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[Block] = []
    page = 1

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, doc)
            text = _paragraph_text(paragraph)
            if not text:
                images = _paragraph_images(paragraph, doc, images_dir)
                blocks.extend(images)
                continue

            style = (paragraph.style.name if paragraph.style else "") or ""
            level = _heading_level(paragraph, style)
            if _is_page_break(paragraph):
                page += 1
            kind = "heading" if level else ("list" if _is_list(paragraph, style) else "paragraph")
            blocks.append(Block(kind=kind, text=text, level=level, page=page))
            for image_block in _paragraph_images(paragraph, doc, images_dir):
                image_block.page = page
                blocks.append(image_block)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            rows = _table_rows(table)
            if rows:
                blocks.append(Block(kind="table", table=rows, page=page))

    title = (doc.core_properties.title or "").strip()
    if not title:
        title = next((b.text for b in blocks if b.kind == "heading"), path.stem)
    sections = build_sections(blocks, fallback_title=title)
    return Document(
        doc_id=_slug(path.stem),
        title=title,
        source_path=str(path),
        source_type="docx",
        version=version,
        sections=sections,
        page_count=page,
        metadata={"paragraphs": len(blocks)},
    )


def _paragraph_text(paragraph) -> str:
    parts: list[str] = []
    for node in paragraph._p.iter():
        if node.tag.endswith("}t") and node.text:
            parts.append(node.text)
        elif node.tag.endswith("}tab"):
            parts.append("\t")
        elif node.tag.endswith("}br"):
            parts.append("\n")
    return " ".join("".join(parts).split())


def _hyperlink_target(node) -> str:
    for key, value in node.attrib.items():
        if key.endswith("}id"):
            return value
    return ""


def _heading_level(paragraph, style: str) -> int:
    lowered = style.strip().lower()
    if lowered in {"title", "заголовок", "заголовок документа"}:
        return 1
    match = re.search(r"(?:heading|заголовок)\s*([1-9])", style, re.I)
    if match:
        return int(match.group(1))
    ppr = paragraph._p.pPr
    if ppr is not None:
        outline = ppr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}outlineLvl")
        if outline is not None:
            try:
                return int(outline.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")) + 1
            except (TypeError, ValueError):
                pass
    return 0


def _is_list(paragraph, style: str) -> bool:
    if re.search(r"list|список", style, re.I):
        return True
    ppr = paragraph._p.pPr
    return ppr is not None and ppr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr") is not None


def _is_page_break(paragraph) -> bool:
    for node in paragraph._p.iter():
        if node.tag.endswith("}br") and node.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type") == "page":
            return True
    return False


def _paragraph_images(paragraph, doc, images_dir: Path) -> list[Block]:
    result: list[Block] = []
    for blip in paragraph._p.iter():
        if not blip.tag.endswith("}blip"):
            continue
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if not rid:
            continue
        part = doc.part.related_parts.get(rid)
        if part is None:
            continue
        blob = part.blob
        if len(blob) < 500:
            continue
        ext = _image_ext(blob)
        digest = hashlib.sha256(blob).hexdigest()[:16]
        path = images_dir / f"{digest}.{ext}"
        if not path.exists():
            path.write_bytes(blob)
        result.append(
            Block(
                kind="image",
                image_path=str(path),
                caption=path.stem,
                meta={"sha256": hashlib.sha256(blob).hexdigest(), "source": "docx"},
            )
        )
    return result


def _image_ext(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG"):
        return "png"
    if blob.startswith(b"\xff\xd8"):
        return "jpg"
    if blob.startswith(b"GIF8"):
        return "gif"
    return "bin"


def _table_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            text = " ".join(cell.text.split())
            if not cells or cells[-1] != text:
                cells.append(text)
        if any(cells):
            rows.append(cells)
    return rows


def build_sections(blocks: list[Block], fallback_title: str) -> list[Section]:
    sections: list[Section] = []
    path: list[str] = []
    current: Section | None = None

    for block in blocks:
        if block.kind == "heading":
            level = block.level or 1
            while len(path) >= level:
                path.pop()
            path.append(block.text)
            current = Section(title=block.text, level=level, path=list(path), page=block.page)
            sections.append(current)
        else:
            if current is None:
                current = Section(title=fallback_title, level=0, path=[fallback_title], page=block.page)
                sections.append(current)
            current.blocks.append(block)
    return [s for s in sections if s.blocks or s.title]
