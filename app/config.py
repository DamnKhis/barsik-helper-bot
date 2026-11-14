"""Конфигурация приложения. Все значения берутся из переменных окружения / .env."""

from __future__ import annotations

from functools import lru_cache

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
    db_path: str = "data/bars.db"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # Документы администратора
    uploaded_docs_dir: str = "uploaded_docs"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
