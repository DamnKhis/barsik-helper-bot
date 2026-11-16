"""Разбор решённого обращения поддержки (``support_answers/KAN-21.md``).

Файл формирует ``app/bot/services/support_answers.py``: заголовок тикета,
``## История диалога с ИИ`` (до эскалации), блоки ``## Вопрос N`` / ``## Ответ
поддержки N`` и, если пар не нашлось, ``## Диалог``. Картинки лежат рядом в
``attachments/<ISSUE_KEY>/`` и вставлены в Markdown относительными ссылками
(их разбирает ``markdown_parser``).

Что здесь важно: в поиск идёт только то, что реально учит бота — пары
«Вопрос N / Ответ поддержки N». «История диалога с ИИ» и «Диалог» содержат
шумные реплики вида «оаоаво» и персональные данные пользователя (имя, @username,
Telegram ID), поэтому по умолчанию они не индексируются. Режим задаёт
``SUPPORT_INDEX_MODE``:

* ``dialogs`` (по умолчанию) — пары «Вопрос N / Ответ поддержки N», а если их
  нет — блок «Диалог», но **только когда в нём есть реплики поддержки**; тикет
  без ответа (поддержка ещё не ответила) в сборку не попадает;
* ``qa`` — строго пары «Вопрос N / Ответ поддержки N»;
* ``answers`` — только блоки «Ответ поддержки N»;
* ``all`` — весь файл как есть (для отладки).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..models import Document, Section
from .markdown_parser import parse_markdown

NOISE_SECTIONS = ("история диалога с ии",)
ANSWER_PREFIXES = ("ответ поддержки",)
QA_PREFIXES = ("вопрос ", "ответ поддержки")
DIALOG_PREFIXES = ("диалог",)
# Реплики поддержки в блоке «Диалог»: `**Поддержка:** текст` (см. support_answers.py).
REPLY_MARKERS = ("**поддержка:**", "поддержка:")
ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]+-\d+)\b")
MODES = {"qa", "dialogs", "answers", "all"}


def support_index_mode(default: str = "dialogs") -> str:
    """Режим индексации тикета: qa | answers | all."""
    value = (os.getenv("SUPPORT_INDEX_MODE", "") or default).strip().lower()
    return value if value in MODES else default


def parse_support_answer(
    path: Path,
    images_dir: Path,
    version: str,
    mode: str | None = None,
) -> Document:
    """Тикет → документ с ``kind="support"`` и отфильтрованными разделами."""
    document = parse_markdown(path, images_dir, version)
    mode = (mode or support_index_mode()).lower()

    if mode != "all":
        document.sections = _filter_sections(document.sections, mode)

    document.kind = "support"
    document.metadata["support_mode"] = mode
    issue_key = _issue_key(path, document)
    if issue_key:
        document.metadata["issue_key"] = issue_key
    return document


def _filter_sections(sections: list[Section], mode: str) -> list[Section]:
    if mode == "all":
        return sections
    if mode == "answers":
        return [s for s in sections if _starts_with(s, ANSWER_PREFIXES)]
    if mode == "dialogs":
        pairs = [s for s in sections if _starts_with(s, QA_PREFIXES)]
        if pairs:
            return pairs
        # Диалог берём, только если поддержка в нём что-то ответила: иначе
        # индексировать нечего, а шумные реплики пользователя только мешают.
        return [
            section
            for section in sections
            if _starts_with(section, DIALOG_PREFIXES) and _has_support_reply(section)
        ]
    # qa (по умолчанию): только пары «Вопрос N / Ответ поддержки N»
    return [s for s in sections if _starts_with(s, QA_PREFIXES)]


def _is_noise(section: Section) -> bool:
    path = " > ".join(section.path).lower()
    return any(noise in path for noise in NOISE_SECTIONS)


def _has_support_reply(section: Section) -> bool:
    text = "\n".join(block.text or "" for block in section.blocks).lower()
    return any(marker in text for marker in REPLY_MARKERS)


def _starts_with(section: Section, prefixes: tuple[str, ...]) -> bool:
    if _is_noise(section):
        return False
    title = (section.title or "").strip().lower()
    return any(title.startswith(prefix) for prefix in prefixes)


def _issue_key(path: Path, document: Document) -> str:
    """Ключ тикета: из строки «- Тикет: KAN-21», заголовка или имени файла."""
    for section in document.sections:
        for block in section.blocks:
            match = re.search(r"Тикет:\s*([A-Za-z0-9]+-\d+)", block.text or "")
            if match:
                return match.group(1)
    match = ISSUE_KEY_RE.match((document.title or "").strip())
    if match:
        return match.group(1)
    match = ISSUE_KEY_RE.match(path.stem)
    return match.group(1) if match else ""
