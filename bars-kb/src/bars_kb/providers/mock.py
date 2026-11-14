from __future__ import annotations

from pathlib import Path
from typing import Any


class MockLLM:
    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        return {}


class MockVision:
    def describe_image(self, image_path: Path, prompt: str) -> dict[str, Any]:
        return {}
