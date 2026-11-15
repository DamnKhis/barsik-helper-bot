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
    postgres_schema: str = "barsik"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # Документы администратора
    uploaded_docs_dir: str = "uploaded_docs"

    # Вопросы со скриншотами: vision-разбор через тот же ключ, что у RAG.
    # IMAGE_QUERY_DIR — куда складывать присланные картинки (нужны в тикетах
    # оператора), VISION_ENABLED=0 — отвечать только по подписи.
    vision_enabled: bool = True
    vision_model: str = ""
    vision_cache_dir: str = "data/vision_cache"
    image_query_dir: str = "data/query_images"
    image_max_mb: float = 20.0
    image_query_keep_days: int = 30
    send_kb_images: bool = False

    # Дашборд аналитики: HTTP Basic. Пустой пароль = страница открыта всем.
    dashboard_user: str = "admin"
    dashboard_password: str = ""

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
