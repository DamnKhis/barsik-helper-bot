from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from .config import Settings
from .exports import read_latest


logger = logging.getLogger(__name__)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def repo_root(path: Path) -> Path:
    result = _git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{path} is not inside a git repository")
    return Path(result.stdout.strip())


def current_branch(root: Path) -> str:
    """Return the branch to push to, even when CI checks out a detached HEAD."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    if branch and branch != "HEAD":
        return branch

    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        for name in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            value = os.getenv(name, "").strip()
            if value:
                return value
        ref = os.getenv("GITHUB_REF", "").strip()
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):]

    raise RuntimeError("cannot publish from a detached HEAD")


def _ensure_real_build(manifest: dict, allow_mock: bool) -> None:
    if allow_mock:
        return
    embedding = (manifest.get("embedding") or {}).get("provider")
    llm = (manifest.get("enrichment") or {}).get("llm_provider")
    if embedding == "hash" or llm == "mock":
        raise RuntimeError(
            "refusing to publish a mock/hash build: it would replace the real handoff "
            "package. Pass --allow-mock if this is intentional."
        )


def publish_dist(
    settings: Settings,
    build_id: str | None = None,
    message: str | None = None,
    push: bool = True,
    allow_mock: bool = False,
) -> dict:
    """Stage, commit and push the dist handoff package.

    Only paths inside ``dist/`` are staged. Other staged changes are rejected so
    the automation cannot accidentally commit unrelated work.
    """
    build_id = build_id or read_latest(settings.dist_dir)
    if not build_id:
        raise RuntimeError("dist/LATEST is missing; build the KB first")

    build_dir = settings.dist_dir / build_id
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"build manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _ensure_real_build(manifest, allow_mock)

    root = repo_root(settings.root)
    try:
        dist_rel = settings.dist_dir.resolve().relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"dist directory is outside the git repository: {settings.dist_dir}") from exc
    dist_arg = dist_rel.as_posix()

    staged_before = [line for line in _git(root, "diff", "--cached", "--name-only").stdout.splitlines() if line]
    outside = [
        path for path in staged_before
        if path != dist_arg and not path.startswith(dist_arg + "/")
    ]
    if outside:
        raise RuntimeError(
            "refusing to publish: unrelated files are already staged: " + ", ".join(outside)
        )

    _git(root, "add", "-A", "--", dist_arg)
    changed = [
        line for line in _git(root, "diff", "--cached", "--name-only", "--", dist_arg).stdout.splitlines()
        if line
    ]

    result = {
        "build_id": build_id,
        "committed": False,
        "pushed": False,
        "files": changed,
    }
    if not changed:
        logger.info("dist is already committed for build %s", build_id)
        return result

    branch = current_branch(root)

    identity: list[str] = []
    if not _git(root, "config", "user.name", check=False).stdout.strip():
        identity += ["-c", "user.name=BARS KB Bot"]
    if not _git(root, "config", "user.email", check=False).stdout.strip():
        identity += ["-c", "user.email=bars-kb@users.noreply.github.com"]

    commit_message = message or f"chore: update KB package {build_id}"
    _git(root, *identity, "commit", "-m", commit_message)
    result["commit"] = _git(root, "rev-parse", "HEAD").stdout.strip()
    result["committed"] = True

    if push:
        remote = os.getenv("GIT_PUBLISH_REMOTE", "origin")
        _git(root, "push", remote, f"HEAD:refs/heads/{branch}")
        result["pushed"] = True

    logger.info(
        "published %s: %d files, commit %s%s",
        build_id,
        len(changed),
        result["commit"][:12],
        f", pushed to {remote}/{branch}" if result["pushed"] else "",
    )
    return result
