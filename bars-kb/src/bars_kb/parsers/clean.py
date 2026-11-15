from __future__ import annotations

import re

FOOTER_PATTERNS = [
    re.compile(r"^\s*Ошибка!\s*Используйте вкладку", re.I),
    re.compile(r"^\s*должен здесь отображаться", re.I),
    re.compile(r"^\s*–\s*\d+\s*$"),
    re.compile(r"^\s*Table of Contents\s*[–-]\s*\d+\s*$", re.I),
    re.compile(r"^\s*Exported on\s+\d", re.I),
    re.compile(r"^\s*ЕИС\s*[–-]\s*", re.I),
    re.compile(r"^\s*\d{1,3}\s*$"),
]

TOC_LINE = re.compile(r"\.{3,}\s*\d+\s*$")


def clean_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    skip_next_footer = False
    for raw in lines:
        line = raw.replace("\u00a0", " ").rstrip()
        if skip_next_footer:
            skip_next_footer = False
            if FOOTER_PATTERNS[1].search(line):
                continue
        if any(p.search(line) for p in FOOTER_PATTERNS):
            if FOOTER_PATTERNS[0].search(line):
                skip_next_footer = True
            continue
        if TOC_LINE.search(line):
            continue
        if not line.strip():
            if result and result[-1] != "":
                result.append("")
            continue
        result.append(line)
    return result


def is_toc_page(lines: list[str]) -> bool:
    return sum(1 for line in lines if TOC_LINE.search(line)) >= 5


def clean_text(text: str) -> str:
    lines = clean_lines(text.splitlines())
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        if not line.strip():
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
        else:
            buffer.append(line.strip())
    if buffer:
        paragraphs.append(" ".join(buffer))
    return "\n\n".join(p.strip() for p in paragraphs if p.strip())


def is_boilerplate(text: str) -> bool:
    value = text.strip()
    if not value:
        return True
    if any(p.search(value) for p in FOOTER_PATTERNS):
        return True
    if value.lower() in {"содержание", "table of contents"}:
        return True
    return False
