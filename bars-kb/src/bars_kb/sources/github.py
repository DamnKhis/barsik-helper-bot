from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from ..config import Settings
from ..parsers import SUPPORTED


API = "https://api.github.com"


def sync_github(settings: Settings) -> Path:
    if not settings.github_repo:
        raise RuntimeError("GITHUB_REPO is not set")
    root = settings.data_dir / "github" / settings.github_branch
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "bars-kb-ingest",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    tree = _get_json(
        f"{API}/repos/{settings.github_repo}/git/trees/{settings.github_branch}?recursive=1",
        headers,
    )
    files = [
        item
        for item in tree.get("tree", [])
        if item.get("type") == "blob"
        and item.get("path", "").startswith(settings.github_docs_path.rstrip("/") + "/")
        and Path(item["path"]).suffix.lower() in SUPPORTED
    ]
    if not files:
        raise RuntimeError(f"No supported documents under {settings.github_docs_path}/")

    for item in files:
        relative = Path(item["path"])
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw_headers = {**headers, "Accept": "application/vnd.github.raw"}
        request = urllib.request.Request(
            f"{API}/repos/{settings.github_repo}/contents/{item['path']}?ref={settings.github_branch}",
            headers=raw_headers,
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            target.write_bytes(response.read())
    (root / "_github_tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    return root


def _get_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
