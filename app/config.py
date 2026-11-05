from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://support:support@127.0.0.1:5432/support"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
