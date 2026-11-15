from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

LATEST_NAME = "LATEST"
BUILD_ID_RE = re.compile(r"^\d{8}T\d{6}_[0-9a-f]{8}$")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_build_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and bool(BUILD_ID_RE.match(path.name))
        and (path / "manifest.json").exists()
    )


def build_ids(dist_dir: Path) -> list[str]:
    """Return build ids in dist/, oldest first (the id starts with a timestamp)."""
    if not dist_dir.exists():
        return []
    return sorted(path.name for path in dist_dir.iterdir() if is_build_dir(path))


def list_builds(dist_dir: Path) -> list[dict]:
    builds: list[dict] = []
    for build_id in build_ids(dist_dir):
        manifest = _read_json(dist_dir / build_id / "manifest.json")
        source = manifest.get("source") or {}
        builds.append(
            {
                "build_id": build_id,
                "created_at": manifest.get("created_at", ""),
                "counts": manifest.get("counts", {}),
                "source_digest": source.get("digest", ""),
            }
        )
    return builds


def read_latest(dist_dir: Path) -> str | None:
    """Read the stable ``dist/LATEST`` pointer."""
    path = dist_dir / LATEST_NAME
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        return None
    if not BUILD_ID_RE.match(value):
        logger.warning("ignoring invalid %s: %r", path, value)
        return None
    if not is_build_dir(dist_dir / value):
        logger.warning("%s points to a missing build: %s", path, value)
        return None
    return value


def write_latest(dist_dir: Path, build_id: str) -> Path:
    """Atomically point ``dist/LATEST`` at a build."""
    if not BUILD_ID_RE.match(build_id):
        raise ValueError(f"invalid build id: {build_id}")
    dist_dir.mkdir(parents=True, exist_ok=True)
    target = dist_dir / LATEST_NAME
    temporary = target.with_suffix(".tmp")
    temporary.write_text(build_id + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def latest_manifest(dist_dir: Path) -> dict | None:
    build_id = read_latest(dist_dir)
    if not build_id:
        return None
    manifest = _read_json(dist_dir / build_id / "manifest.json")
    return manifest or None


def previous_build(dist_dir: Path, current: str | None = None) -> str | None:
    """Return the build before ``current`` (or the second newest build)."""
    builds = build_ids(dist_dir)
    if not builds:
        return None
    if current in builds:
        index = builds.index(current)
        return builds[index - 1] if index > 0 else None
    return builds[-2] if len(builds) > 1 else None


def prune_builds(dist_dir: Path, keep: int = 2, current: str | None = None) -> list[str]:
    """Delete old build directories, always keeping ``current``."""
    keep = max(1, int(keep))
    builds = build_ids(dist_dir)
    if not builds:
        return []
    keep_set = set(builds[-keep:])
    if current:
        keep_set.add(current)
    removed: list[str] = []
    for build_id in builds:
        if build_id in keep_set:
            continue
        shutil.rmtree(dist_dir / build_id)
        removed.append(build_id)
        logger.info("pruned old build %s", build_id)
    return removed


def resolve_export_dir(path: str | Path | None = None, root: Path | None = None) -> Path:
    """Resolve an export path to a concrete build directory.

    Accepts a build directory, the ``dist`` directory with a ``LATEST`` pointer,
    or a ``dist`` directory with several builds. Relative paths are looked up
    next to the current directory and next to the project root.
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
        ]
        base = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    else:
        base = base.resolve()

    build_id = read_latest(base)
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
