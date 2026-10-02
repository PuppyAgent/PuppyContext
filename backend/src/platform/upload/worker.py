"""
ETL ARQ Worker Settings

Handles file ETL jobs (OCR + postprocess for document processing).

Run worker:
  uv run arq src.platform.upload.worker.WorkerSettings
"""

from __future__ import annotations

# Load .env file before any other imports that need env vars
from pathlib import Path

from dotenv import load_dotenv

# Find .env relative to this file (backend/.env)
# worker.py -> upload/ -> platform/ -> src/ -> backend/
_env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_env_path, override=False)


import logging

from arq.connections import RedisSettings

from src.infra.llm.service import LLMService
from src.infra.s3.service import S3Service
from src.infra.file_processing.config import etl_config
from src.platform.upload.jobs import (
    etl_finalize_upload_job,
    etl_ocr_job,
    etl_postprocess_job,
)
from src.infra.file_processing.ocr import get_ocr_provider
from src.infra.file_processing.state.repository import ETLStateRepositoryRedis
from src.infra.file_processing.tasks.repository import ETLTaskRepositorySupabase

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    """
    Initialize services for ETL jobs.
    """
    # ========== ETL Services ==========
    ctx["s3_service"] = S3Service()
    ctx["llm_service"] = LLMService()

    # Use pluggable OCR provider (configured via OCR_PROVIDER env var)
    # Supports: 'mineru', 'reducto'
    ocr_provider = get_ocr_provider()
    ctx["ocr_provider"] = ocr_provider
    # Keep legacy key for backward compatibility
    ctx["mineru_client"] = ocr_provider

    ctx["task_repository"] = ETLTaskRepositorySupabase()

    # ETL Redis runtime state repo (shares same Redis as ARQ)
    ctx["state_repo"] = ETLStateRepositoryRedis(ctx["redis"])
    ctx["arq_queue_name"] = etl_config.etl_arq_queue_name

    logger.info(f"ETL ARQ worker startup complete (OCR provider: {ocr_provider.name})")


async def shutdown(ctx: dict) -> None:
    """
    Cleanup on worker shutdown.
    """
    logger.info("ETL ARQ worker shutdown")


class WorkerSettings:
    # ``etl_finalize_upload_job`` lives alongside the OCR/postprocess
    # jobs because it shares the same worker context (S3 service, task
    # repo, runtime state repo) and Redis queue. It's invoked after a
    # browser-direct-to-S3 upload completes; see
    # ``platform.upload.jobs.etl_finalize_upload_job`` for the flow.
    functions = [etl_ocr_job, etl_postprocess_job, etl_finalize_upload_job]  # noqa: RUF012
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(etl_config.etl_redis_url)
    queue_name = etl_config.etl_arq_queue_name
    # NOTE: ARQ cancels jobs on timeout via asyncio.CancelledError (BaseException on Py3.12).
    # Keep this in sync with MineRU/LLM latency expectations.
    job_timeout = etl_config.etl_task_timeout
