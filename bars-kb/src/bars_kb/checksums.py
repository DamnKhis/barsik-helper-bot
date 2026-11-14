from __future__ import annotations

import hashlib
from pathlib import Path


def write_checksums(root: Path) -> Path:
    """Write sha256 checksums for every file in a handoff package."""
    lines: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == "checksums.sha256":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.relative_to(root).as_posix()}")
    target = root / "checksums.sha256"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
