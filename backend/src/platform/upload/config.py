"""Upload owns queue, timeout, retry and runtime-state policy.

ETL_* environment names and the ``etl`` queue are retained intentionally: these
are deployed Upload processing settings, not Import/Synchronize configuration.
The shared Redis URL is inherited from the neutral transport configuration.
"""
from pydantic import Field
from src.infra.queue_config import QueueConnectionConfig


class UploadWorkerConfig(QueueConnectionConfig):
    etl_queue_size: int = Field(default=30, ge=1)
    etl_worker_count: int = Field(default=3, ge=1)
    etl_task_timeout: int = Field(default=600, ge=1)
    etl_arq_queue_name: str = "etl"
    etl_redis_prefix: str = "etl:"
    etl_state_ttl_seconds: int = Field(default=24 * 60 * 60, ge=1)
    etl_state_terminal_ttl_seconds: int = Field(default=60 * 60, ge=1)
    etl_ocr_max_attempts: int = Field(default=3, ge=1)
    etl_postprocess_max_attempts: int = Field(default=3, ge=1)
    etl_retry_backoff_base_seconds: int = Field(default=2, ge=1)
    etl_retry_backoff_max_seconds: int = Field(default=60, ge=1)


upload_config = UploadWorkerConfig()
