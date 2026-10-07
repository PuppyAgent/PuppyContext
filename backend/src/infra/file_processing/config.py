"""Reusable OCR and transformation settings, never a lifecycle/queue policy."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ETLConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", env_ignore_empty=True)
    ocr_provider: str = Field(default="mineru", description="OCR provider to use")
    etl_cache_dir: str = ".mineru_cache"
    etl_rules_dir: str = ".etl_rules"
    etl_postprocess_chunk_threshold_chars: int = 50_000
    etl_postprocess_chunk_size_chars: int = 12_000
    etl_postprocess_max_chunks: int = 20
    etl_global_rule_enabled: bool = True
    etl_global_rule_id: int | None = None


etl_config = ETLConfig()
