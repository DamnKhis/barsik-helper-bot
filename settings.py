"""Единые настройки всех сервисов из переменных окружения / .env.

Один файл на весь репозиторий: и бот, и API, и индексатор читают одни и те же
имена переменных (см. .env.example). Имена переменных — контракт (AGENTS.md,
п.2): менять только всем вместе.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram-бот (роль 3) ---
    bot_token: str = ""
    operator_chat_id: str = ""
    api_url: str = "http://localhost:8000"

    # --- LLM: любой OpenAI-совместимый провайдер ---
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # --- Эмбеддинги (пустые значения наследуют LLM_BASE_URL / LLM_API_KEY) ---
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536  # bge-m3 -> 1024

    # --- Хранилища ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "bars_docs"
    redis_url: str = "redis://localhost:6379/0"

    # --- RAG (роль 2) ---
    top_k: int = 5
    confidence_threshold: float = 0.45

    # --- Git-интеграция («Будущие возможности» п.2) ---
    docs_repo_url: str = ""
    docs_repo_branch: str = "main"
    docs_repo_token: str = ""
    docs_poll_interval_sec: int = 60
    github_webhook_secret: str = ""
    gitlab_webhook_secret: str = ""

    # --- Пути ---
    data_dir: str = "./data"
    docs_dir: str = "./docs"

    @property
    def db_path(self) -> str:
        return f"{self.data_dir.rstrip('/')}/analytics.db"

    @property
    def embedding_endpoint(self) -> str:
        return self.embedding_base_url or self.llm_base_url

    @property
    def embedding_key(self) -> str:
        return self.embedding_api_key or self.llm_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
