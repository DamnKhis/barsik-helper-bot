from __future__ import annotations

from ..config import Settings
from ..providers.deepseek_qwen import DeepSeekLLM, QwenVision


def build_llm(settings: Settings):
    if settings.llm_provider == "deepseek" and settings.deepseek_api_key:
        return DeepSeekLLM(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )
    return None


def build_vision(settings: Settings):
    if settings.vision_provider == "qwen" and settings.qwen_api_key:
        return QwenVision(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            model=settings.qwen_vision_model,
        )
    return None
