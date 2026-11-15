"""Конфигурация приложения. Все значения берутся из переменных окружения / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    bot_token: str = ""
    admin_ids: str = ""

    # Хранилища
    database_url: str = "sqlite+aiosqlite:///./data/bars.db"
    postgres_schema: str = "barsik"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # Документы администратора
    uploaded_docs_dir: str = "uploaded_docs"

    # Дашборд аналитики: HTTP Basic. Пустой пароль = страница открыта всем.
    dashboard_user: str = "admin"
    dashboard_password: str = ""

    # Jira: поддержка живёт в задачах Jira, бот синхронизирует диалог.
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("JIRA_API_TOKEN", "JIRO_API_KEY"),
    )
    jira_project_key: str = ""
    jira_issue_type: str = "Task"
    jira_poll_interval_sec: int = 30
    jira_sync: bool = True

    @property
    def admin_id_set(self) -> set[int]:
        """ADMIN_IDS в .env: '111,222' или '111 222' -> {111, 222}."""
        result: set[int] = set()
        for raw in self.admin_ids.replace(",", " ").split():
            try:
                result.add(int(raw))
            except ValueError:
                continue
        return result

    @property
    def jira_enabled(self) -> bool:
        return bool(self.jira_base_url.strip() and self.jira_api_token.strip())

    @property
    def jira_create_ready(self) -> bool:
        return self.jira_enabled and bool(self.jira_project_key.strip())

    @property
    def jira_sync_enabled(self) -> bool:
        return self.jira_enabled and self.jira_sync


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
