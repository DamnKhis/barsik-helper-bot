"""Сохранение присланных пользователем изображений.

Картинки не удаляются после ответа: они нужны другой части проекта — когда ИИ
не может ответить и вопрос уходит оператору, оператор должен видеть весь диалог
вместе со скриншотами (эта часть появится позже, здесь только раскладка файлов).

Раскладка:

    {IMAGE_QUERY_DIR}/{telegram_id}/{YYYYmmddTHHMMSS}_{uuid8}.{ext}

Путь к файлу возвращается вызывающему коду, поэтому связать изображение с
историей диалога можно и позже (в `dialog_history.image_path` или в тикете).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

# Чистим старое не чаще одного раза за процесс.
_pruned = False


def image_dir() -> Path:
    """Каталог для присланных картинок (``IMAGE_QUERY_DIR``)."""
    path = Path(settings.image_query_dir).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def max_image_bytes() -> int:
    return int(max(1.0, float(settings.image_max_mb)) * 1024 * 1024)


def guess_suffix(mime_type: str | None, file_name: str | None = None) -> str | None:
    """Расширение файла по MIME-типу или имени; ``None`` — формат не поддержан."""
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime in MIME_SUFFIXES:
        return MIME_SUFFIXES[mime]
    suffix = Path(file_name or "").suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def is_image_message(message) -> bool:
    """Сообщение содержит фото или изображение-файл (Telegram-типы aiogram)."""
    if getattr(message, "photo", None):
        return True
    document = getattr(message, "document", None)
    return guess_suffix(getattr(document, "mime_type", None), getattr(document, "file_name", None)) is not None


def pick_extension(message) -> str:
    """Расширение для сохраняемого файла: фото Telegram всегда JPEG."""
    if getattr(message, "photo", None):
        return ".jpg"
    document = getattr(message, "document", None)
    return guess_suffix(getattr(document, "mime_type", None), getattr(document, "file_name", None)) or ".jpg"


def pick_file(message):
    """Самый крупный вариант фото либо документ-изображение."""
    photos = getattr(message, "photo", None)
    if photos:
        return photos[-1]
    return getattr(message, "document", None)


async def save_query_image(bot, message, user_id: int) -> Path:
    """Скачать картинку из сообщения и сохранить её на диск.

    Возвращает путь к файлу. Файл остаётся на диске: он понадобится оператору.
    """
    suffix = pick_extension(message)
    target_dir = image_dir() / str(user_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = target_dir / f"{stamp}_{uuid4().hex[:8]}{suffix}"

    await bot.download(pick_file(message), destination=target)

    size = target.stat().st_size if target.exists() else 0
    if size == 0:
        target.unlink(missing_ok=True)
        raise ValueError("Telegram отдал пустой файл")
    if size > max_image_bytes():
        target.unlink(missing_ok=True)
        raise ValueError(f"Изображение больше {max_image_bytes() // (1024 * 1024)} МБ")

    prune_query_images()
    logger.info("Изображение вопроса сохранено: %s (%s КБ)", target, size // 1024)
    return target


def prune_query_images(max_age_days: int | None = None) -> list[Path]:
    """Удалить старые изображения вопросов.

    Возраст — ``IMAGE_QUERY_KEEP_DAYS`` (по умолчанию 30, ``0`` — хранить вечно).
    Вызывается лениво, не чаще одного раза за процесс.
    """
    global _pruned
    if max_age_days is None:
        max_age_days = int(settings.image_query_keep_days)
    if _pruned or max_age_days <= 0:
        return []

    _pruned = True
    root = image_dir()
    if not root.is_dir():
        return []

    threshold = time.time() - timedelta(days=max_age_days).total_seconds()
    removed: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < threshold:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    if removed:
        logger.info("Удалено старых изображений вопросов: %s", len(removed))
    return removed
