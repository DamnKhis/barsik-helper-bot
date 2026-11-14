"""Stable resolution of the handoff package path.

The build id changes on every rebuild, so clients should never hardcode
``dist/<build_id>``. Point ``KB_EXPORT_DIR`` at the ``dist`` directory (or leave
it empty) and this helper follows ``dist/LATEST``.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LATEST_NAME = "LATEST"


def resolve_export_dir(path: str | Path | None = None) -> Path:
    if path is None or str(path).strip() == "":
        path = os.getenv("KB_EXPORT_DIR", "").strip() or "dist"

    base = Path(path).expanduser()
    if not base.is_absolute():
        candidates = [
            (Path.cwd() / base).resolve(),
            (PROJECT_ROOT / base).resolve(),
            (PROJECT_ROOT.parent / base).resolve(),
        ]
        base = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    else:
        base = base.resolve()

    latest = base / LATEST_NAME
    if latest.is_file():
        build_id = latest.read_text(encoding="utf-8").strip()
        if build_id:
            base = base / build_id

    if (base / "collection_config.json").exists():
        return base
    if base.is_dir():
        builds = sorted(
            candidate
            for candidate in base.iterdir()
            if (candidate / "collection_config.json").exists()
        )
        if builds:
            return builds[-1]
    return base
