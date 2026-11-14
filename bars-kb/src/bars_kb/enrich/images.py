from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import Settings
from ..providers.base import VisionProvider
from .cache import EnrichmentCache


PROMPT_VISUAL = """Ты анализируешь изображение из документации БАРС-Офис.
Верни строго JSON без markdown по схеме:
{
  "kind": "screenshot|diagram|other",
  "description": "что изображено, где находится элемент, что означает",
  "ocr_text": "весь читаемый текст с изображения",
  "ui_elements": [{"label": "подпись", "location": "где находится", "callout": 1}],
  "diagram_mermaid": "graph TD; ... (только для схем)",
  "nodes": [{"id": "n1", "label": "..."}],
  "edges": [{"from": "n1", "to": "n2", "label": "..."}],
  "questions": ["вопросы пользователей, на которые отвечает изображение"]
}
Не выдумывай подписи. Если текст не читается, укажи это в description.
Пиши по-русски."""

PROMPT_DIAGRAM = """Ты анализируешь блок-схему бизнес-процесса из документации БАРС.
Верни строго JSON без markdown по схеме:
{
  "kind": "diagram",
  "description": "пошаговое описание процесса: кто, что и когда делает",
  "ocr_text": "все подписи",
  "nodes": [{"id": "n1", "label": "подпись", "type": "start|task|gateway|role|end"}],
  "edges": [{"from": "n1", "to": "n2", "label": "условие"}],
  "diagram_mermaid": "graph TD; ...",
  "questions": ["кто согласовывает", "что происходит при отказе"]
}
Не выдумывай узлы и связи. Пиши по-русски."""


def enrich_image(
    settings: Settings,
    vision: VisionProvider | None,
    cache: EnrichmentCache,
    image_path: Path,
    caption: str,
    context: str,
    kind_hint: str = "screenshot",
) -> dict:
    if not image_path.exists():
        return _fallback(caption, context, kind_hint)
    if vision is None or settings.mock_vision:
        return _fallback(caption, context, kind_hint)

    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    prompt = PROMPT_DIAGRAM if kind_hint == "diagram" else PROMPT_VISUAL
    key = cache.key("image", digest, prompt[:100], settings.qwen_vision_model)
    cached = cache.get(key)
    if cached:
        return cached
    result = vision.describe_image(image_path, prompt + f"\n\nКонтекст: {context[:2000]}")
    normalized = {
        "kind": str(result.get("kind", kind_hint))[:40],
        "description": str(result.get("description", ""))[:2000],
        "ocr_text": str(result.get("ocr_text", ""))[:4000],
        "ui_elements": result.get("ui_elements", [])[:50] if isinstance(result.get("ui_elements"), list) else [],
        "nodes": result.get("nodes", [])[:100] if isinstance(result.get("nodes"), list) else [],
        "edges": result.get("edges", [])[:200] if isinstance(result.get("edges"), list) else [],
        "diagram_mermaid": str(result.get("diagram_mermaid", ""))[:6000],
        "questions": [str(x)[:300] for x in result.get("questions", [])][:10]
        if isinstance(result.get("questions"), list)
        else [],
    }
    cache.set(key, normalized)
    return normalized


def _fallback(caption: str, context: str, kind: str) -> dict:
    label = caption or "изображение"
    return {
        "kind": kind,
        "description": f"{label}. Иллюстрация к разделу: {context[:300]}",
        "ocr_text": "",
        "ui_elements": [],
        "nodes": [],
        "edges": [],
        "diagram_mermaid": "",
        "questions": [],
    }
