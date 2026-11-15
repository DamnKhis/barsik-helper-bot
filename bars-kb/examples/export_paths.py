"""Stable resolution of the handoff package path.

The build id changes on every rebuild, so clients should never hardcode
``dist/<build_id>``. Point ``KB_EXPORT_DIR`` at the ``dist`` directory (or leave
it empty) and this helper follows ``dist/LATEST``.

``resolve_export_dir`` is intentionally cheap and re-reads ``LATEST`` on every
call, so a long-running process picks up a new build (or a rollback) without a
restart.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LATEST_NAME = "LATEST"


def resolve_base_dir(path: str | Path | None = None, root: Path | None = None) -> Path:
    """Resolve the configured directory without following ``LATEST``.

    Returns the ``dist`` directory, or a concrete build directory when the
    caller passed one explicitly.
    """
    if path is None or str(path).strip() == "":
        path = os.getenv("KB_EXPORT_DIR", "").strip() or "dist"

    base = Path(path).expanduser()
    if not base.is_absolute():
        project_root = (root or Path.cwd()).resolve()
        candidates = [
            (Path.cwd() / base).resolve(),
            (project_root / base).resolve(),
            (project_root.parent / base).resolve(),
            (PROJECT_ROOT / base).resolve(),
        ]
        base = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    else:
        base = base.resolve()
    return base


def latest_build_id(path: str | Path | None = None) -> str | None:
    """Return the build id from ``dist/LATEST`` if it points to an existing build."""
    base = resolve_base_dir(path)
    latest = base / LATEST_NAME
    if not latest.is_file():
        return None
    build_id = latest.read_text(encoding="utf-8").strip()
    if not build_id:
        return None
    if not (base / build_id / "collection_config.json").exists():
        return None
    return build_id


def resolve_export_dir(path: str | Path | None = None, root: Path | None = None) -> Path:
    """Resolve an export path to a concrete build directory.

    Accepts a build directory, the ``dist`` directory with a ``LATEST`` pointer,
    or a ``dist`` directory with several builds (newest wins as a fallback).
    """
    base = resolve_base_dir(path, root)

    build_id = latest_build_id(base)
    if build_id:
        return (base / build_id).resolve()

    if (base / "collection_config.json").exists():
        return base
    if base.is_dir():
        builds = sorted(
            candidate
            for candidate in base.iterdir()
            if (candidate / "collection_config.json").exists()
        )
        if builds:
            return builds[-1].resolve()
    return base
