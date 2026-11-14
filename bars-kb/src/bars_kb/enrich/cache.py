from __future__ import annotations

import hashlib
import json
from pathlib import Path


class EnrichmentCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, *parts: str) -> str:
        value = "\x1f".join(parts).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def get(self, key: str) -> dict | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: dict) -> None:
        path = self.root / f"{key}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
