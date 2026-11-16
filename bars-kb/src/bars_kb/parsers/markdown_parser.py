from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from ..models import Block, Document
from .docx_parser import _slug, build_sections
from .image_filter import inspect_image

logger = logging.getLogger(__name__)


def resolve_image(link: str, base_dir: Path, images_dir: Path) -> tuple[Path, str] | None:
    """Найти картинку по ссылке из Markdown и положить копию в ``images_dir``.

    Ссылки в Markdown относительные (например
    ``attachments/KAN-21/20261115T234029_843476c3.jpg``), поэтому ищем файл
    рядом с самим документом. Копия нужна потому, что дальше пайплайн ищет
    картинки относительно ``data/`` и иначе чанк с изображением терялся бы.
    """
    raw = link.split()[0].strip().strip("<>")
    if "://" in raw:  # внешняя ссылка, файла рядом нет
        return None
    target = Path(raw)
    candidate = target if target.is_absolute() else base_dir / target
    if not candidate.is_file():
        logger.debug("markdown image not found: %s", candidate)
        return None
    blob = candidate.read_bytes()
    verdict = inspect_image(blob)
    if not verdict.ok:
        logger.debug("skip markdown image %s: %s", candidate.name, verdict.reason)
        return None
    digest = hashlib.sha256(blob).hexdigest()
    suffix = ".png" if blob.startswith(b"\x89PNG") else ".jpg"
    images_dir.mkdir(parents=True, exist_ok=True)
    copy = images_dir / f"{digest[:16]}{suffix}"
    if not copy.exists():
        copy.write_bytes(blob)
    return copy, digest


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
            resolved = resolve_image(image.group(2), path.parent, images_dir)
            if resolved is None:
                paragraph.append(line.strip())  # ссылку оставляем текстом
                continue
            copied, digest = resolved
            blocks.append(
                Block(
                    kind="image",
                    caption=image.group(1).strip() or copied.stem,
                    image_path=str(copied),
                    meta={"sha256": digest, "source": "markdown"},
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
