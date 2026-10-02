"""
ETL Configuration

Configuration settings for ETL service.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ETLConfig(BaseSettings):
    """Configuration for ETL service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    # ========== OCR Provider Settings ==========
    # Supported providers: 'mineru', 'reducto'
    ocr_provider: str = Field(
        default="mineru",
        description="OCR provider to use: 'mineru' or 'reducto'",
    )

    # Queue settings
    etl_queue_size: int = Field(default=30, description="Maximum ETL queue size")

    etl_worker_count: int = Field(default=3, description="Number of ETL worker tasks")

    # Task timeout
    etl_task_timeout: int = Field(
        default=600, description="ETL task timeout in seconds (10 minutes)"
    )

    # Cache and storage directories
    etl_cache_dir: str = Field(
        default=".mineru_cache", description="Directory for MineRU cache"
    )

    etl_rules_dir: str = Field(
        default=".etl_rules", description="Directory for ETL rules"
    )

    # Redis connection uses the deployed ETL_REDIS_URL name. Import/Synchronize
    # read that same transport URL through infra.queue_config; their queue and
    # timeout policy lives in their own domain config, never in this module.
    # Upload still uses the ETL queue/runtime settings below during its staged
    # processing extraction. IMPORT_REDIS_URL is not supported.
    etl_redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for ARQ (shared by ETL + import workers, "
                    "separated by queue name; IMPORT_REDIS_URL is not used)",
    )

    etl_redis_prefix: str = Field(
        default="etl:", description="Redis key prefix for ETL runtime state"
    )

    etl_state_ttl_seconds: int = Field(
        default=24 * 60 * 60, description="TTL for ETL runtime state in Redis (seconds)"
    )

    etl_state_terminal_ttl_seconds: int = Field(
        default=60 * 60, description="TTL for terminal state cache in Redis (seconds)"
    )

    etl_arq_queue_name: str = Field(
        default="etl", description="ARQ queue name for ETL jobs"
    )

    etl_ocr_max_attempts: int = Field(
        default=3, description="Maximum attempts for OCR stage"
    )

    etl_postprocess_max_attempts: int = Field(
        default=3, description="Maximum attempts for postprocess stage"
    )

    etl_retry_backoff_base_seconds: int = Field(
        default=2, description="Base seconds for retry backoff"
    )

    etl_retry_backoff_max_seconds: int = Field(
        default=60, description="Maximum seconds for retry backoff"
    )

    etl_postprocess_chunk_threshold_chars: int = Field(
        default=50_000,
        description="Markdown size threshold to switch to chunk strategy",
    )

    etl_postprocess_chunk_size_chars: int = Field(
        default=12_000, description="Chunk size for chunked strategies"
    )

    etl_postprocess_max_chunks: int = Field(
        default=20, description="Maximum number of chunks for chunked strategies"
    )

    etl_global_rule_enabled: bool = Field(
        default=True, description="Enable built-in global default ETL rule"
    )

    etl_global_rule_id: int | None = Field(
        default=None,
        description="Optional database rule_id to use as global default rule (if configured)",
    )


# Global config instance
etl_config = ETLConfig()
