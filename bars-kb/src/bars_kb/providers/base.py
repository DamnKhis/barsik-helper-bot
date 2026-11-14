from __future__ import annotations

from pathlib import Path
from typing import Protocol


class LLMProvider(Protocol):
    def complete_json(self, system: str, user: str) -> dict:
        ...


class VisionProvider(Protocol):
    def describe_image(self, image_path: Path, prompt: str) -> dict:
        ...


class EmbeddingProvider(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...
