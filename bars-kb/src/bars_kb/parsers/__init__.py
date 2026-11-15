from __future__ import annotations

from pathlib import Path

from ..models import Document
from .docx_parser import parse_docx
from .markdown_parser import parse_markdown
from .pdf_parser import parse_pdf
from .text_parser import parse_text

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt"}


def parse_document(path: Path, images_dir: Path, version: str) -> Document:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path, images_dir, version)
    if suffix == ".docx":
        return parse_docx(path, images_dir, version)
    if suffix in {".md", ".markdown"}:
        return parse_markdown(path, images_dir, version)
    if suffix == ".txt":
        return parse_text(path, images_dir, version)
    raise ValueError(f"Unsupported format: {path.suffix}")
