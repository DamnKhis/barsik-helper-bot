from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path

from PIL import Image

try:  # PyMuPDF >= 1.24 exposes the canonical `pymupdf` module.
    import pymupdf
except ImportError:  # pragma: no cover - compatibility with older wheels
    import fitz as pymupdf

from ..models import Block, Document
from .clean import clean_lines, is_toc_page
from .docx_parser import _slug, build_sections
from .image_filter import inspect_image

logger = logging.getLogger(__name__)

NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,3})\s+([А-ЯЁA-Z][^\n]{1,140})$")
BULLET = re.compile(r"^\s*(?:[•●▪◦o]|[-–—]|\d+[.)])\s+")
BARE_BULLET = re.compile(r"^\s*[•●▪◦]\s*$")

# Vertical gaps larger than this are treated as paragraph breaks. PyMuPDF
# returns one block per paragraph; the gap keeps footer lines adjacent while
# still separating real paragraphs.
BLOCK_GAP = 3.0

WEB_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}


def parse_pdf(path: Path, images_dir: Path, version: str) -> Document:
    """Parse a PDF without external tools (no pdftotext/pdfimages/poppler)."""

    images_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[Block] = []
    title = path.stem

    with pymupdf.open(path) as doc:
        page_images = _extract_images(doc, images_dir)
        page_count = doc.page_count

        for page_no, page in enumerate(doc, start=1):
            lines = _page_lines(page)
            if page_no == 1:
                first = next((line.strip() for line in lines if line.strip()), "")
                if first:
                    title = first
            if is_toc_page(lines):
                continue

            blocks.extend(_blocks_from_lines(clean_lines(lines), page_no))
            for image_block in page_images.get(page_no, []):
                image_block.page = page_no
                blocks.append(image_block)

    sections = build_sections(blocks, fallback_title=title)
    for section in sections:
        for block in section.blocks:
            if block.kind == "image" and not block.caption:
                block.caption = f"Иллюстрация к разделу «{section.title}»"

    return Document(
        doc_id=_slug(path.stem),
        title=title,
        source_path=str(path),
        source_type="pdf",
        version=version,
        sections=sections,
        page_count=page_count,
        metadata={"images": sum(len(items) for items in page_images.values())},
    )


def _page_lines(page) -> list[str]:
    """Return page lines in reading order, preserving heading indentation."""

    page_blocks = page.get_text("dict", sort=True).get("blocks", [])
    left = min(
        (
            line["bbox"][0]
            for block in page_blocks
            if block.get("type") == 0
            for line in block.get("lines", [])
            if _line_text(line).strip()
        ),
        default=0.0,
    )

    lines: list[str] = []
    previous_bottom: float | None = None
    for block in page_blocks:
        bbox = block.get("bbox") or (0.0, 0.0, 0.0, 0.0)
        if block.get("type") != 0:
            # Image blocks are added separately; keep the surrounding text
            # paragraphs from being glued together across the image.
            if lines and lines[-1] != "":
                lines.append("")
            previous_bottom = bbox[3]
            continue

        if lines and previous_bottom is not None and bbox[1] - previous_bottom > BLOCK_GAP:
            if lines[-1] != "":
                lines.append("")

        block_lines: list[tuple[str, float]] = []
        for line in block.get("lines", []):
            text = _line_text(line)
            if text.strip():
                block_lines.append((text, line["bbox"][0]))

        for text, x0 in _merge_bullets(block_lines):
            indent = " " if x0 > left + 6.0 else ""
            lines.append(f"{indent}{text}")
        previous_bottom = bbox[3]

    return lines


def _line_text(line: dict) -> str:
    text = "".join(span.get("text", "") for span in line.get("spans", []))
    return text.replace("\u00a0", " ").rstrip()


def _merge_bullets(lines: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Merge a standalone bullet glyph with the wrapped lines that follow it.

    PyMuPDF emits bullet markers as their own line, unlike `pdftotext -layout`
    which keeps the marker and text on one line. The rest of the parser expects
    the latter shape.
    """

    merged: list[tuple[str, float]] = []
    pending: tuple[str, float] | None = None

    for text, x0 in lines:
        if BARE_BULLET.match(text):
            if pending is not None:
                merged.append(pending)
            pending = (text.strip(), x0)
        elif pending is not None:
            pending = (f"{pending[0]} {text.strip()}", pending[1])
        else:
            merged.append((text, x0))

    if pending is not None:
        merged.append(pending)
    return merged


def _blocks_from_lines(lines: list[str], page_no: int) -> list[Block]:
    """Turn cleaned text lines into headings, list items and paragraphs."""

    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        match = NUMBERED_HEADING.match(line) if not line.startswith(" ") else None
        if match and len(stripped) < 150 and not stripped.endswith((".", ":", ";")):
            blocks.append(
                Block(
                    kind="heading",
                    text=stripped,
                    level=match.group(1).count(".") + 1,
                    page=page_no,
                )
            )
            index += 1
            continue

        if BULLET.match(line):
            blocks.append(Block(kind="list", text=stripped, page=page_no))
            index += 1
            continue

        # Join wrapped paragraph lines until a blank line or a structural line.
        buffer = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index]
            if not nxt.strip() or BULLET.match(nxt):
                break
            if not nxt.startswith(" ") and NUMBERED_HEADING.match(nxt):
                break
            buffer.append(nxt.strip())
            index += 1
        blocks.append(Block(kind="paragraph", text=" ".join(buffer), page=page_no))

    return blocks


def _extract_images(doc, images_dir: Path) -> dict[int, list[Block]]:
    """Extract embedded raster images with PyMuPDF and filter out noise."""

    result: dict[int, list[Block]] = {}
    seen: dict[int, tuple[Path, str] | None] = {}

    for page_no, page in enumerate(doc, start=1):
        infos = page.get_images(full=True)
        masks = {info[1] for info in infos if len(info) > 1 and info[1]}
        blocks: list[Block] = []

        for info in infos:
            xref = info[0]
            if not xref or xref in masks:
                continue
            if xref in seen:
                cached = seen[xref]
                if cached is None:
                    continue
                target, digest = cached
                blocks.append(_image_block(target, page_no, digest))
                continue

            try:
                extracted = doc.extract_image(xref)
                blob = extracted.get("image", b"")
                ext = str(extracted.get("ext", "png")).lower()
            except Exception as exc:  # malformed PDFs can raise many types
                logger.debug("skip PDF image xref=%s on page %s: %s", xref, page_no, exc)
                seen[xref] = None
                continue

            verdict = inspect_image(blob)
            if not verdict.ok:
                logger.debug(
                    "skip PDF image xref=%s on page %s: %s",
                    xref,
                    page_no,
                    verdict.reason,
                )
                seen[xref] = None
                continue

            digest = hashlib.sha256(blob).hexdigest()
            target = _save_image(blob, ext, images_dir, digest[:16])
            seen[xref] = (target, digest)
            blocks.append(_image_block(target, page_no, digest))

        if blocks:
            result[page_no] = blocks

    return result


def _save_image(blob: bytes, ext: str, images_dir: Path, stem: str) -> Path:
    if ext in WEB_IMAGE_EXTENSIONS:
        target = images_dir / f"{stem}.{ext}"
        if not target.exists():
            target.write_bytes(blob)
        return target

    # Formats such as JPX/JBIG2 are not reliably supported by browsers or
    # vision APIs, so normalize them to PNG.
    target = images_dir / f"{stem}.png"
    if not target.exists():
        with Image.open(io.BytesIO(blob)) as image:
            image.save(target, "PNG")
    return target


def _image_block(path: Path, page_no: int, digest: str) -> Block:
    return Block(
        kind="image",
        image_path=str(path),
        caption="",
        meta={"sha256": digest, "page": page_no, "source": "pdf"},
    )
