from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    root: Path
    data_dir: Path
    inbox: Path
    images_dir: Path
    qdrant_path: Path
    dist_dir: Path
    # Решённые обращения поддержки — второй источник рядом с документацией.
    support_inbox: Path = Path("docs/support_answers")
    support_index_mode: str = "dialogs"
    llm_provider: str = "mock"
    vision_provider: str = "mock"
    embedding_provider: str = "hash"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://openrouter.ai/api/v1"
    qwen_llm_model: str = "qwen/qwen3.8-flash"
    qwen_vision_model: str = "qwen/qwen3.8-flash"
    qwen_embedding_model: str = "qwen/qwen3-embedding-8b"
    qwen_enable_thinking: bool = False
    embedding_dim: int = 1024
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    collection_prefix: str = "bars_kb"
    image_base_url: str = ""
    chunk_max_chars: int = 1400
    chunk_overlap_chars: int = 180
    parent_max_chars: int = 6000
    enrichment_workers: int = 4
    github_repo: str = ""
    github_branch: str = "main"
    github_token: str = ""
    github_docs_path: str = "docs"
    extra: dict[str, str] = field(default_factory=dict)

    def source_roots(self) -> list[tuple[Path, str]]:
        """Папки-источники: документация и решённые обращения поддержки."""
        roots = [(self.inbox, "doc")]
        if self.support_inbox != self.inbox:
            roots.append((self.support_inbox, "support"))
        return roots

    @property
    def mock_llm(self) -> bool:
        return self.llm_provider == "mock" or not self.qwen_api_key

    @property
    def mock_vision(self) -> bool:
        return self.vision_provider == "mock" or not self.qwen_api_key

    @property
    def mock_embedding(self) -> bool:
        return self.embedding_provider == "hash" or not self.qwen_api_key


def load_settings(root: Path | None = None) -> Settings:
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    _load_env(root / ".env")
    # In the monorepo bars-kb/ is a subproject and the shared .env lives in the
    # repository root. Local values win because _load_env uses setdefault.
    parent_env = root.parent / ".env"
    if parent_env.is_file() and parent_env != root / ".env":
        _load_env(parent_env)

    def p(name: str, default: str) -> Path:
        value = Path(os.getenv(name, default))
        return value if value.is_absolute() else root / value

    def s(name: str, default: str) -> str:
        return os.getenv(name, default)

    def i(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    def b(name: str, default: bool) -> bool:
        return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

    return Settings(
        root=root,
        data_dir=p("DATA_DIR", "data"),
        inbox=p("DOCS_PATH", "docs/source"),
        support_inbox=p("SUPPORT_DOCS_PATH", "docs/support_answers"),
        support_index_mode=s("SUPPORT_INDEX_MODE", "dialogs"),
        images_dir=p("IMAGES_DIR", "data/images"),
        qdrant_path=p("QDRANT_PATH", "data/qdrant"),
        dist_dir=root / "dist",
        llm_provider=s("LLM_PROVIDER", "mock"),
        vision_provider=s("VISION_PROVIDER", "mock"),
        embedding_provider=s("EMBEDDING_PROVIDER", "hash"),
        qwen_api_key=s("QWEN_API_KEY", ""),
        qwen_base_url=s("QWEN_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        qwen_llm_model=s("QWEN_LLM_MODEL", "qwen/qwen3.8-flash"),
        qwen_vision_model=s("QWEN_VISION_MODEL", "qwen/qwen3.8-flash"),
        qwen_embedding_model=s("QWEN_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b"),
        qwen_enable_thinking=b("QWEN_ENABLE_THINKING", False),
        embedding_dim=i("EMBEDDING_DIM", 1024),
        qdrant_url=s("QDRANT_URL", "").rstrip("/"),
        qdrant_api_key=s("QDRANT_API_KEY", ""),
        collection_prefix=s("COLLECTION_PREFIX", "bars_kb"),
        image_base_url=s("IMAGE_BASE_URL", "").rstrip("/"),
        chunk_max_chars=i("CHUNK_MAX_CHARS", 1400),
        chunk_overlap_chars=i("CHUNK_OVERLAP_CHARS", 180),
        parent_max_chars=i("PARENT_MAX_CHARS", 6000),
        enrichment_workers=i("ENRICHMENT_WORKERS", 4),
        github_repo=s("GITHUB_REPO", ""),
        github_branch=s("GITHUB_BRANCH", "main"),
        github_token=s("GITHUB_TOKEN", ""),
        github_docs_path=s("GITHUB_DOCS_PATH", "docs"),
    )
