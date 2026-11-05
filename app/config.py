from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://support:support@127.0.0.1:5432/support"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = Field(default="gpt-4.1-mini", min_length=1)
    worker_parallelism: int = Field(default=32, ge=1, le=60)
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")


@lru_cache
def get_settings() -> Settings:
    return Settings()
