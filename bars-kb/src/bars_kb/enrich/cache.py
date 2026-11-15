from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class EnrichmentCache:
    """File-backed cache shared by parallel enrichment workers.

    ``lock(key)`` serializes work for one cache key, so two images with the
    same content cannot trigger duplicate vision requests. File I/O itself is
    protected separately because different keys write different files.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._io_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    def key(self, *parts: str) -> str:
        value = "\x1f".join(parts).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        with self._key_locks_guard:
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:
            yield

    def get(self, key: str) -> dict | None:
        path = self.root / f"{key}.json"
        with self._io_lock:
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def set(self, key: str, value: dict) -> None:
        path = self.root / f"{key}.json"
        with self._io_lock:
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
