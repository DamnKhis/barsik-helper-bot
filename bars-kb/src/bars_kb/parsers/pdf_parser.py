from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from ..models import Block, Document, Section
from .clean import clean_lines, is_toc_page
from .docx_parser import _slug, build_sections


NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,3})\s+([А-ЯЁA-Z][^\n]{1,140})$")
BULLET = re.compile(r"^\s*(?:[•●▪◦o]|[-–—]|\d+[.)])\s+")
TABLE_SPLIT = re.compile(r"\s{2,}")
NUMERIC_CELL = re.compile(r"\d")


def parse_pdf(path: Path, images_dir: Path, version: str) -> Document:
    raw = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pages = raw.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()

    images_dir.mkdir(parents=True, exist_ok=True)
    page_images = _extract_images(path, images_dir, _slug(path.stem))
    blocks: list[Block] = []
    title = path.stem

    for page_no, page_text in enumerate(pages, start=1):
        lines = page_text.splitlines()
        if page_no == 1:
            first = next((ln.strip() for ln in lines if ln.strip()), "")
            if first:
                title = first
        if is_toc_page(lines):
            continue

        cleaned = clean_lines(lines)
        # PDF table heuristics are intentionally disabled for now: Word exports
        # lose column boundaries and produce misleading rows. DOCX tables are
        # parsed structurally; PDF text remains a fallback.
        table_ranges: list[tuple[int, int]] = []
        table_lines: set[int] = set()

        table_index = 0
        idx = 0
        while idx < len(cleaned):
            line = cleaned[idx]
            if idx in table_lines:
                start, end = next((s, e) for s, e in table_ranges if s <= idx < e)
                rows = _table_rows(cleaned[start:end])
                if rows:
                    blocks.append(Block(kind="table", table=rows, page=page_no))
                idx = end
                continue

            stripped = line.strip()
            if not stripped:
                idx += 1
                continue

            match = NUMBERED_HEADING.match(line) if not line.startswith(" ") else None
            if match and len(stripped) < 150 and not stripped.endswith((".", ":", ";")):
                blocks.append(Block(kind="heading", text=stripped, level=match.group(1).count(".") + 1, page=page_no))
                idx += 1
                continue

            if BULLET.match(line):
                blocks.append(Block(kind="list", text=stripped, page=page_no))
                idx += 1
                continue

            # Join wrapped paragraph lines until a blank line or a structural line.
            buffer = [stripped]
            idx += 1
            while idx < len(cleaned):
                nxt = cleaned[idx]
                if not nxt.strip() or idx in table_lines or BULLET.match(nxt):
                    break
                if not nxt.startswith(" ") and NUMBERED_HEADING.match(nxt):
                    break
                buffer.append(nxt.strip())
                idx += 1
            blocks.append(Block(kind="paragraph", text=" ".join(buffer), page=page_no))

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
        page_count=len(pages),
        metadata={"images": sum(len(v) for v in page_images.values())},
    )


def _table_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(lines):
        if not _is_table_line(lines[start]):
            start += 1
            continue
        end = start
        while end < len(lines) and _is_table_line(lines[end]):
            end += 1
        if end - start >= 2:
            ranges.append((start, end))
        start = end
    return ranges


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or BULLET.match(stripped):
        return False
    cells = [c for c in TABLE_SPLIT.split(stripped) if c.strip()]
    return len(cells) >= 3 and any(NUMERIC_CELL.search(c) for c in cells)


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        cells = [c.strip() for c in TABLE_SPLIT.split(line.strip()) if c.strip()]
        if len(cells) >= 2:
            rows.append(cells)
    return rows


def _extract_images(path: Path, images_dir: Path, prefix: str) -> dict[int, list[Block]]:
    result: dict[int, list[Block]] = {}
    try:
        listing = subprocess.run(
            ["pdfimages", "-list", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return result

    page_has_images: set[int] = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        page = int(parts[0])
        kind = parts[2]
        try:
            width, height = int(parts[3]), int(parts[4])
        except ValueError:
            continue
        if kind == "smask" or width < 100 or height < 60:
            continue
        page_has_images.add(page)

    for page in sorted(page_has_images):
        out_prefix = images_dir / f"{prefix}-p{page}"
        for old in images_dir.glob(f"{prefix}-p{page}-*"):
            old.unlink()
        try:
            subprocess.run(
                ["pdfimages", "-png", "-f", str(page), "-l", str(page), str(path), str(out_prefix)],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        blocks: list[Block] = []
        for candidate in sorted(images_dir.glob(f"{prefix}-p{page}-*")):
            blob = candidate.read_bytes()
            if len(blob) < 2000:
                candidate.unlink()
                continue
            digest = hashlib.sha256(blob).hexdigest()[:16]
            target = images_dir / f"{digest}.png"
            if not target.exists():
                candidate.replace(target)
            else:
                candidate.unlink()
            blocks.append(
                Block(
                    kind="image",
                    image_path=str(target),
                    caption="",
                    meta={"sha256": hashlib.sha256(blob).hexdigest(), "page": page, "source": "pdf"},
                )
            )
        if blocks:
            result[page] = blocks
    return result
