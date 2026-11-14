"""Git-провайдеры: единый интерфейс для GitHub и GitLab. Роль 4.

Зачем абстракция: бриф требует «продуманности будущих интеграций
(GitLab/GitHub)». Логика реиндексации знает только GitProvider — добавление
новой платформы = один адаптер ~60 строк, ядро не меняется.

API: https://docs.github.com/rest и https://docs.gitlab.com/ee/api/
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from abc import ABC, abstractmethod
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DOC_EXTENSIONS = (".docx", ".md", ".txt")


class GitProvider(ABC):
    def __init__(self, repo_url: str, branch: str, token: str):
        self.repo_url = repo_url.rstrip("/").removesuffix(".git")
        self.branch = branch
        self.token = token

    @abstractmethod
    def latest_commit_sha(self) -> str | None: ...

    @abstractmethod
    def list_doc_files(self) -> list[str]: ...

    @abstractmethod
    def download_file(self, path: str) -> bytes: ...


class GithubProvider(GitProvider):
    """https://github.com/<owner>/<repo> -> api.github.com/repos/<owner>/<repo>."""

    def __init__(self, repo_url: str, branch: str, token: str):
        super().__init__(repo_url, branch, token)
        repo_path = self.repo_url.split("github.com/")[-1]
        self.api_base = f"https://api.github.com/repos/{repo_path}"
        self.headers = {"Accept": "application/vnd.github.raw"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def latest_commit_sha(self) -> str | None:
        response = httpx.get(f"{self.api_base}/commits/{self.branch}", headers=self.headers, timeout=15)
        response.raise_for_status()
        return response.json().get("sha")

    def list_doc_files(self) -> list[str]:
        response = httpx.get(
            f"{self.api_base}/git/trees/{self.branch}?recursive=1", headers=self.headers, timeout=15
        )
        response.raise_for_status()
        tree = response.json().get("tree", [])
        return [
            item["path"]
            for item in tree
            if item["type"] == "blob" and item["path"].lower().endswith(DOC_EXTENSIONS)
        ]

    def download_file(self, path: str) -> bytes:
        response = httpx.get(
            f"{self.api_base}/contents/{path}?ref={self.branch}", headers=self.headers, timeout=30
        )
        response.raise_for_status()
        return response.content


class GitlabProvider(GitProvider):
    """GitLab (SaaS или self-hosted): /api/v4/projects/<url-encoded-path>/..."""

    def __init__(self, repo_url: str, branch: str, token: str):
        super().__init__(repo_url, branch, token)
        tail = self.repo_url.split("//")[-1]
        host = tail.split("/")[0]
        project_path = tail.split("/", 1)[1]
        self.api_base = f"https://{host}/api/v4/projects/{quote(project_path, safe='')}"
        self.headers = {"PRIVATE-TOKEN": token} if token else {}

    def latest_commit_sha(self) -> str | None:
        response = httpx.get(
            f"{self.api_base}/repository/commits/{quote(self.branch, safe='')}",
            headers=self.headers,
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("id")

    def list_doc_files(self) -> list[str]:
        files: list[str] = []
        page = 1
        while True:
            response = httpx.get(
                f"{self.api_base}/repository/tree",
                params={"ref": self.branch, "recursive": "true", "per_page": 100, "page": page},
                headers=self.headers,
                timeout=15,
            )
            response.raise_for_status()
            batch = response.json()
            files += [
                item["path"]
                for item in batch
                if item["type"] == "blob" and item["path"].lower().endswith(DOC_EXTENSIONS)
            ]
            if len(batch) < 100:
                return files
            page += 1

    def download_file(self, path: str) -> bytes:
        response = httpx.get(
            f"{self.api_base}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": self.branch},
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.content


def make_provider(repo_url: str, branch: str, token: str) -> GitProvider:
    if "github" in repo_url:
        return GithubProvider(repo_url, branch, token)
    return GitlabProvider(repo_url, branch, token)


# --- Разбор вебхуков ---


def extract_github_push(payload: dict) -> dict:
    return {
        "branch": (payload.get("ref") or "").removeprefix("refs/heads/"),
        "files": _changed_doc_files(payload.get("commits", [])),
    }


def extract_gitlab_push(payload: dict) -> dict:
    return {
        "branch": (payload.get("ref") or "").removeprefix("refs/heads/"),
        "files": _changed_doc_files(payload.get("commits", [])),
    }


def _changed_doc_files(commits: list[dict]) -> list[str]:
    """added + modified (removed не переиндексируем) только среди файлов документации."""
    return [
        path
        for commit in commits
        for path in commit.get("added", []) + commit.get("modified", [])
        if path.lower().endswith(DOC_EXTENSIONS)
    ]


def verify_github_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """X-Hub-Signature-256: HMAC-SHA256 от тела запроса секретом вебхука."""
    if not secret:
        return True  # секрет не настроен — только для локальной отладки
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return signature is not None and hmac.compare_digest(expected, signature)
