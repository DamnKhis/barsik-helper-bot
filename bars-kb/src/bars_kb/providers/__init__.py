from __future__ import annotations

from ..config import Settings
from .qwen import QwenChat


def build_llm(settings: Settings):
    if settings.llm_provider == "qwen" and settings.qwen_api_key:
        return QwenChat(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            model=settings.qwen_llm_model,
            enable_thinking=settings.qwen_enable_thinking,
        )
    return None


def build_vision(settings: Settings):
    if settings.vision_provider == "qwen" and settings.qwen_api_key:
        return QwenChat(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            model=settings.qwen_vision_model,
            enable_thinking=settings.qwen_enable_thinking,
        )
    return None
