"""Понимание присланного изображения и приведение вопроса к одному формату.

Зачем модуль: в базе знаний `bars-kb` скриншоты и схемы уже проиндексированы —
их `raw_text`/`enriched_text` собраны vision-моделью `qwen/qwen3.8-flash` по
промпту ``PROMPT_VISUAL`` из ``bars_kb/enrich/images.py``. Чтобы вопрос со
скриншотом попадал в ту же область векторного пространства, мы описываем
присланную картинку **тем же промптом и той же схемой** и собираем строку
запроса по тому же шаблону, что и чанк скриншота:

    Изображение: <подпись>
    <description>

    Текст на изображении:
    <ocr_text>

    Элементы интерфейса:
    - <label> (<location>)

    Вопросы: <questions>

Для текстового вопроса ``build_query`` возвращает текст без изменений, поэтому
в поиск в обоих случаях уходит обычная строка одного и того же формата:
``Retriever.answer_query(build_query(...))``. Новых коллекций, векторов и
изменений в ``app/rag`` не требуется.

Ключ и base_url берутся из тех же переменных, что у RAG-ядра
(``LLM_API_KEY`` → ``QWEN_API_KEY`` → ``OPENROUTER_API_KEY``, ``LLM_BASE_URL``),
так что один ключ OpenRouter обслуживает и эмбеддинги, и генерацию, и vision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KB_SRC = PROJECT_ROOT / "bars-kb" / "src"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "vision_cache"
DEFAULT_MODEL = "qwen/qwen3.8-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Ключи RAG-модуля (app/rag/retriever.py) читаются из окружения — делаем так же,
# чтобы один и тот же QWEN_API_KEY/LLM_API_KEY обслуживал поиск, ответ и vision.
load_dotenv()

# Ограничение длины запроса: длинный «мусор» из OCR портит и эмбеддинг, и промпт LLM.
MAX_QUERY_CHARS = 3000
MAX_CAPTION_CHARS = 500
MAX_DESCRIPTION_CHARS = 1200
MAX_OCR_CHARS = 1000
MAX_UI_ELEMENTS = 12
MAX_QUESTIONS = 6

_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}


@dataclass(slots=True)
class ImageUnderstanding:
    """Результат vision-разбора присланной картинки."""

    ok: bool = False
    kind: str = "screenshot"
    description: str = ""
    ocr_text: str = ""
    ui_elements: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    diagram_mermaid: str = ""
    model: str = ""
    vision_ms: int = 0
    error: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, model: str = "", vision_ms: int = 0) -> "ImageUnderstanding":
        ui_elements = payload.get("ui_elements")
        questions = payload.get("questions")
        return cls(
            ok=True,
            kind=str(payload.get("kind") or "screenshot")[:40],
            description=str(payload.get("description") or "")[:2000],
            ocr_text=str(payload.get("ocr_text") or "")[:4000],
            ui_elements=[e for e in ui_elements if isinstance(e, dict)][:50] if isinstance(ui_elements, list) else [],
            questions=[str(q) for q in questions][:10] if isinstance(questions, list) else [],
            diagram_mermaid=str(payload.get("diagram_mermaid") or "")[:6000],
            model=model,
            vision_ms=vision_ms,
        )

    @classmethod
    def failed(cls, error: str) -> "ImageUnderstanding":
        return cls(ok=False, error=error[:300])

    def as_cache_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("vision_ms", None)
        data.pop("error", None)
        return data


def vision_enabled() -> bool:
    return bool(settings.vision_enabled)


def api_key() -> str:
    for name in ("LLM_API_KEY", "QWEN_API_KEY", "OPENROUTER_API_KEY"):
        value = os.getenv(name, "").strip()
        if value and not value.startswith("your_"):
            return value
    return ""


def base_url() -> str:
    return (os.getenv("LLM_BASE_URL") or os.getenv("QWEN_BASE_URL") or DEFAULT_BASE_URL).strip()


def vision_model() -> str:
    return (settings.vision_model or os.getenv("QWEN_VISION_MODEL") or DEFAULT_MODEL).strip()


def cache_dir() -> Path:
    path = Path(settings.vision_cache_dir).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def vision_available() -> bool:
    """Vision включён, ключ задан и модель из bars-kb импортируется."""
    if not vision_enabled() or not api_key():
        return False
    return _load_provider_parts() is not None


def _load_provider_parts() -> tuple[Any, str] | None:
    """Ленивая (и безопасная) загрузка vision-провайдера и промпта из bars-kb.

    Импорт лёгкий: ``bars_kb.providers.qwen`` тянет только ``urllib``/``base64``,
    ``bars_kb.enrich.images`` — промпт, которым собирался индекс. Если bars-kb
    нет, работаем по одной подписи и не падаем.
    """
    if str(KB_SRC) not in sys.path:
        sys.path.insert(0, str(KB_SRC))
    try:
        from bars_kb.enrich.images import PROMPT_VISUAL  # noqa: PLC0415
        from bars_kb.providers.qwen import QwenChat  # noqa: PLC0415
    except Exception as exc:  # bars-kb недоступен — не критично
        logger.warning("vision из bars-kb недоступен: %s", exc)
        return None
    return QwenChat, PROMPT_VISUAL


def cache_key(image_path: Path, prompt: str, model: str) -> str:
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    return hashlib.sha256(f"{digest}|{prompt[:120]}|{model}".encode("utf-8")).hexdigest()[:32]


def _cache_get(key: str) -> dict | None:
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    path = cache_dir() / f"{key}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    with _cache_lock:
        _cache[key] = payload
    return payload


def _cache_set(key: str, value: dict) -> None:
    with _cache_lock:
        _cache[key] = value
    path = cache_dir() / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        logger.debug("Кэш vision не записан: %s", exc)


def describe_image(image_path: Path, caption: str = "") -> ImageUnderstanding:
    """Синхронный vision-разбор картинки (вызывать через ``asyncio.to_thread``).

    Никогда не бросает исключение: при любой проблеме возвращает ``ok=False``,
    и вызывающий код ищет по одной подписи.
    """
    if not vision_enabled():
        return ImageUnderstanding.failed("VISION_ENABLED=0")
    if not image_path.is_file():
        return ImageUnderstanding.failed(f"файл не найден: {image_path}")

    parts = _load_provider_parts()
    if parts is None:
        return ImageUnderstanding.failed("vision-провайдер bars-kb недоступен")
    if not api_key():
        return ImageUnderstanding.failed("нет LLM_API_KEY/QWEN_API_KEY")

    QwenChat, prompt = parts
    try:
        key = cache_key(image_path, prompt, vision_model())
        cached = _cache_get(key)
    except OSError as exc:
        return ImageUnderstanding.failed(f"не читается файл: {exc}")
    if cached:
        return ImageUnderstanding.from_payload(cached, model=vision_model())
    provider = QwenChat(
        api_key=api_key(),
        base_url=base_url(),
        model=vision_model(),
        enable_thinking=False,
    )
    request = prompt
    if caption.strip():
        request += f"\n\nПодпись пользователя к изображению: {caption.strip()[:MAX_CAPTION_CHARS]}"

    started = time.perf_counter()
    try:
        payload = provider.describe_image(image_path, request)
    except Exception as exc:  # сеть/модель — ищем по подписи, не падаем
        return ImageUnderstanding.failed(f"vision не ответил: {exc}")
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    understanding = ImageUnderstanding.from_payload(payload, model=vision_model(), vision_ms=elapsed_ms)
    _cache_set(key, understanding.as_cache_dict())
    logger.info(
        "vision: %s, kind=%s, ocr=%s символов, %s мс",
        image_path.name,
        understanding.kind,
        len(understanding.ocr_text),
        elapsed_ms,
    )
    return understanding


async def describe_image_async(image_path: Path, caption: str = "") -> ImageUnderstanding:
    """Неблокирующая обёртка над ``describe_image``."""
    return await asyncio.to_thread(describe_image, image_path, caption)


def _ui_lines(elements: list[dict]) -> str:
    lines = []
    for element in elements[:MAX_UI_ELEMENTS]:
        label = str(element.get("label") or "").strip()
        if not label:
            continue
        location = str(element.get("location") or "").strip()
        lines.append(f"- {label} ({location})" if location else f"- {label}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def build_query(caption: str | None, understanding: ImageUnderstanding | None = None) -> str:
    """Единая точка нормализации: текст вопроса или картинка → строка для поиска.

    Без картинки возвращает исходный текст (текстовый путь не меняется).
    """
    text = " ".join((caption or "").split())

    if understanding is None:
        return text

    if not understanding.ok:
        if not text:
            return ""
        return _clip(f"Изображение: {text} (скриншот интерфейса БАРС-Офис).", MAX_QUERY_CHARS)

    kind = (understanding.kind or "screenshot").lower()
    label = "Схема бизнес-процесса" if kind == "diagram" else "Изображение"
    header = f"{label}: {text}" if text else f"{label}: скриншот из вопроса пользователя"
    parts = [f"Скриншот интерфейса БАРС-Офис. Вопрос пользователя: {text or 'не указан'}", header]

    description = _clip(understanding.description, MAX_DESCRIPTION_CHARS)
    if description:
        parts.append(description)

    ocr_text = _clip(understanding.ocr_text, MAX_OCR_CHARS)
    if ocr_text:
        parts.append(f"Текст на изображении:\n{ocr_text}")

    ui_lines = _ui_lines(understanding.ui_elements)
    if ui_lines:
        parts.append(f"Элементы интерфейса:\n{ui_lines}")

    if understanding.questions:
        questions = "; ".join(_clip(q, 200) for q in understanding.questions[:MAX_QUESTIONS] if str(q).strip())
        if questions:
            parts.append(f"Вопросы: {questions}")

    return "\n\n".join(part for part in parts if part)[:MAX_QUERY_CHARS]
