"""Сохранение полезных ответов поддержки в Markdown для базы знаний.

Когда тикет в Jira получает «полезный» статус, поллер собирает переписку и
кладёт её в SUPPORT_ANSWERS_DIR (по умолчанию bars-kb/docs/support_answers).
Вложения из тикета скачиваются рядом в attachments/<issue_key>/ и вставляются
в Markdown ссылками/картинками. Файл перезаписывается только при изменениях.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.bot.services.jira import BOT_TRANSCRIPT_PREFIX, BOT_USER_PREFIX, JiraAttachment, JiraComment
from app.config import settings

logger = logging.getLogger(__name__)

USEFUL_STATUS_KEYWORDS = ("полезн", "useful", "helpful")

UNSAFE_FILENAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)
ATTACHMENT_LINE_RE = re.compile(r"\(вложение:\s*([^)]+)\)")


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


def _link_attachments(text: str, attachments: dict[str, tuple[str, bool]]) -> str:
    """Заменить '(вложение: photo.jpg)' на markdown-ссылку или картинку."""
    if not attachments:
        return text

    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        target = attachments.get(name) or attachments.get(Path(name).name)
        if target is None:
            return match.group(0)
        url, is_image = target
        return f"![{name}]({url})" if is_image else f"[{name}]({url})"

    return ATTACHMENT_LINE_RE.sub(repl, text)


def render_support_answer(
    issue_key: str,
    issue_url: str,
    summary: str,
    comments: list[JiraComment],
    attachments: dict[str, tuple[str, bool]] | None = None,
) -> str | None:
    pairs = _extract_pairs(comments)
    if not pairs:
        return None

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

    for index, (question, answer) in enumerate(pairs, 1):
        lines.extend(
            [
                f"## Вопрос {index}",
                _link_attachments(question, attachments or {}),
                "",
                f"## Ответ поддержки {index}",
                _link_attachments(answer, attachments or {}),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


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
    target_dir = Path(settings.support_answers_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    attachments = await _download_attachments(client, issue_key, target_dir)
    content = render_support_answer(issue_key, issue_url, summary, comments, attachments)
    if content is None:
        logger.info("В тикете %s нет пар вопрос/ответ, сохранять нечего", issue_key)
        return None

    target = target_dir / safe_filename(issue_key)
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return target

    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    logger.info("Полезный ответ сохранён: %s", target)
    return target
