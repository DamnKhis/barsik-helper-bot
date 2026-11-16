"""Сохранение полезных ответов поддержки в Markdown для базы знаний.

Когда тикет в Jira получает «полезный» статус, поллер собирает переписку и
кладёт её в SUPPORT_ANSWERS_DIR (по умолчанию bars-kb/docs/support_answers).
Вложения из тикета скачиваются рядом в attachments/<issue_key>/ и вставляются
в Markdown ссылками/картинками. Файл перезаписывается только при изменениях.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from app.bot.services.jira import BOT_TRANSCRIPT_PREFIX, BOT_USER_PREFIX, JiraAttachment, JiraComment
from app.config import settings

logger = logging.getLogger(__name__)

USEFUL_STATUS_KEYWORDS = ("полезн", "useful", "helpful")

UNSAFE_FILENAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)
ATTACHMENT_LINE_RE = re.compile(r"\(вложение:\s*([^)]+)\)")
ATTACHMENT_BANG_RE = re.compile(r"!([^!\n]+)!")
LEGACY_IMAGE_NOTE_RE = re.compile(
    r"📎\s*Скриншот от пользователя:\s*([^\s(]+)(?:\s*\([^)]*\))?"
)


def is_useful_status(status_name: str) -> bool:
    normalized = status_name.strip().lower()
    return any(keyword in normalized for keyword in USEFUL_STATUS_KEYWORDS)


def _strip_user_marker(body: str) -> str:
    """Из комментария вида '[BARS-USER:42]\\nИван: текст' достаём текст."""
    lines = body.splitlines()
    if lines and lines[0].startswith(BOT_USER_PREFIX):
        lines = lines[1:]
    if lines and lines[0].endswith(":"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _extract_pairs(comments: list[JiraComment]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    question: str | None = None
    answers: list[str] = []

    def flush() -> None:
        if question is not None and answers:
            pairs.append((question, "\n\n".join(answers)))

    for comment in comments:
        body = comment.body.strip()
        if not body or body.startswith(BOT_TRANSCRIPT_PREFIX):
            continue
        if body.startswith(BOT_USER_PREFIX):
            flush()
            question = _strip_user_marker(body)
            answers = []
        elif body.startswith("[BARS-"):
            continue
        elif question is not None:
            answers.append(body)
    flush()
    return pairs


def _transcript_text(comments: list[JiraComment]) -> str:
    parts: list[str] = []
    for comment in comments:
        body = comment.body.strip()
        if body.startswith(BOT_TRANSCRIPT_PREFIX):
            text = body[len(BOT_TRANSCRIPT_PREFIX):].strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _dialog_lines(comments: list[JiraComment]) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for comment in comments:
        body = comment.body.strip()
        if not body or body.startswith(BOT_TRANSCRIPT_PREFIX):
            continue
        if body.startswith(BOT_USER_PREFIX):
            lines.append(("Пользователь", _strip_user_marker(body)))
        elif body.startswith("[BARS-"):
            continue
        else:
            lines.append(("Поддержка", body))
    return lines


def _link_attachments(text: str, attachments: dict[str, tuple[str, bool]]) -> str:
    """Заменить упоминания вложений на markdown-ссылки или картинки."""
    if not attachments:
        return text

    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        target = attachments.get(name) or attachments.get(Path(name).name)
        if target is None:
            return match.group(0)
        url, is_image = target
        return f"![{name}]({url})" if is_image else f"[{name}]({url})"

    text = ATTACHMENT_LINE_RE.sub(repl, text)
    text = ATTACHMENT_BANG_RE.sub(repl, text)
    return LEGACY_IMAGE_NOTE_RE.sub(repl, text)


def render_support_answer(
    issue_key: str,
    issue_url: str,
    summary: str,
    comments: list[JiraComment],
    attachments: dict[str, tuple[str, bool]] | None = None,
) -> str | None:
    pairs = _extract_pairs(comments)
    transcript = _transcript_text(comments)
    dialog = _dialog_lines(comments)
    if not pairs and not transcript and not dialog:
        return None

    links = attachments or {}
    lines = [
        f"# {issue_key}: полезный ответ поддержки",
        "",
        f"- Тикет: {issue_key}",
    ]
    if summary:
        lines.append(f"- Тема: {summary}")
    if issue_url:
        lines.append(f"- Ссылка: {issue_url}")
    lines.append("")

    if transcript:
        lines.extend(
            [
                "## История диалога с ИИ",
                _link_attachments(transcript, links),
                "",
            ]
        )

    if pairs:
        for index, (question, answer) in enumerate(pairs, 1):
            lines.extend(
                [
                    f"## Вопрос {index}",
                    _link_attachments(question, links),
                    "",
                    f"## Ответ поддержки {index}",
                    _link_attachments(answer, links),
                    "",
                ]
            )
    else:
        lines.append("## Диалог")
        lines.append("")
        for role, text in dialog:
            lines.append(f"**{role}:** {_link_attachments(text, links)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _inherit_owner(path: Path, reference: Path) -> None:
    """Сделать владельца как у каталога: контейнер пишет от root, а на хосте ждёт mi."""
    try:
        stat = reference.stat()
        os.chown(path, stat.st_uid, stat.st_gid)
    except OSError:
        pass


def _safe_name(value: str, fallback: str) -> str:
    name = UNSAFE_FILENAME_RE.sub("_", Path(value.replace("\\", "/")).name).strip("._")
    return name or fallback


def safe_filename(issue_key: str) -> str:
    return f"{_safe_name(issue_key, 'support_answer')}.md"


async def _download_attachments(
    client,
    issue_key: str,
    target_dir: Path,
) -> dict[str, tuple[str, bool]]:
    """Скачать вложения тикета; вернуть {имя: (относительная ссылка, картинка?)}."""
    try:
        attachments: list[JiraAttachment] = await client.list_attachments(issue_key)
    except Exception as exc:
        logger.warning("Не удалось получить вложения %s: %s", issue_key, exc)
        return {}

    mapping: dict[str, tuple[str, bool]] = {}
    if not attachments:
        return mapping

    folder = _safe_name(issue_key, "issue")
    attach_dir = target_dir / "attachments" / folder
    attach_dir.mkdir(parents=True, exist_ok=True)
    _inherit_owner(attach_dir, target_dir)

    for attachment in attachments:
        filename = _safe_name(attachment.filename, f"file_{attachment.id}")
        destination = attach_dir / filename
        if not destination.exists() and attachment.content_url:
            try:
                content = await client.download(attachment.content_url)
                destination.write_bytes(content)
            except Exception as exc:
                logger.warning(
                    "Не удалось скачать вложение %s из %s: %s",
                    attachment.filename,
                    issue_key,
                    exc,
                )
                continue
        if destination.exists():
            _inherit_owner(destination, target_dir)
            relative = f"attachments/{folder}/{filename}"
            mapping[attachment.filename] = (
                relative,
                (attachment.mime_type or "").startswith("image/"),
            )
    return mapping


async def save_support_answer(
    client,
    issue_key: str,
    issue_url: str,
    summary: str,
    comments: list[JiraComment],
) -> Path | None:
    """Записать полезный ответ. None — если пар вопрос/ответ не нашлось."""
    if render_support_answer(issue_key, issue_url, summary, comments) is None:
        logger.info("В тикете %s нет содержимого для сохранения", issue_key)
        return None

    target_dir = Path(settings.support_answers_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    attachments = await _download_attachments(client, issue_key, target_dir)
    content = render_support_answer(issue_key, issue_url, summary, comments, attachments)

    target = target_dir / safe_filename(issue_key)
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return target

    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    _inherit_owner(tmp, target_dir)
    tmp.replace(target)
    logger.info("Полезный ответ сохранён: %s", target)
    return target
