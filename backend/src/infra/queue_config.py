"""Shared ARQ transport settings, independent of any ingestion lifecycle."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class QueueConnectionConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", env_ignore_empty=True)
    # Preserve the deployed shared transport variable. There is intentionally
    # no IMPORT_REDIS_URL/SYNCHRONIZE_REDIS_URL: isolation is by logical queue.
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="ETL_REDIS_URL")
