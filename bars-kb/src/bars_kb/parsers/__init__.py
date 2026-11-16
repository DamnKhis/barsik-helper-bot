from __future__ import annotations

from pathlib import Path

from ..models import Document
from .docx_parser import parse_docx
from .markdown_parser import parse_markdown
from .pdf_parser import parse_pdf
from .support_answer import parse_support_answer
from .text_parser import parse_text

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt"}


def parse_document(path: Path, images_dir: Path, version: str, kind: str = "doc", support_mode: str = "dialogs") -> Document:
    """Разобрать документ в зависимости от формата и источника (doc/support)."""
    suffix = path.suffix.lower()
    if kind == "support" and suffix in {".md", ".markdown"}:
        return parse_support_answer(path, images_dir, version, mode=support_mode)

    if suffix == ".pdf":
        document = parse_pdf(path, images_dir, version)
    elif suffix == ".docx":
        document = parse_docx(path, images_dir, version)
    elif suffix in {".md", ".markdown"}:
        document = parse_markdown(path, images_dir, version)
    elif suffix == ".txt":
        document = parse_text(path, images_dir, version)
    else:
        raise ValueError(f"Unsupported format: {path.suffix}")

    document.kind = kind
    return document
