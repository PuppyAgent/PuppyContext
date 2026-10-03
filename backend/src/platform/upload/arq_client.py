"""
ETL ARQ Client

API-side helper to enqueue ETL jobs and access the underlying ArqRedis pool.
"""

from __future__ import annotations

import logging

from arq.connections import ArqRedis, RedisSettings, create_pool

from src.platform.upload.config import upload_config

logger = logging.getLogger(__name__)


class UploadArqClient:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        queue_name: str | None = None,
    ):
        self.redis_url = redis_url or upload_config.redis_url
        self.queue_name = queue_name or upload_config.etl_arq_queue_name
        self._pool: ArqRedis | None = None

    async def get_pool(self) -> ArqRedis:
        if self._pool is None:
            settings = RedisSettings.from_dsn(self.redis_url)
            self._pool = await create_pool(settings)
            logger.info("UploadArqClient: redis pool created")
        return self._pool

    async def enqueue_ocr(self, task_id: str | int) -> str:
        redis = await self.get_pool()
        job = await redis.enqueue_job(
            "etl_ocr_job", task_id, _queue_name=self.queue_name
        )
        return job.job_id

    async def enqueue_postprocess(self, task_id: str | int) -> str:
        redis = await self.get_pool()
        job = await redis.enqueue_job(
            "etl_postprocess_job", task_id, _queue_name=self.queue_name
        )
        return job.job_id

    async def enqueue_finalize_upload(self, task_id: str | int) -> str:
        """
        Enqueue the post-upload finalization job for a direct-to-S3
        multipart upload.

        Triggered from ``/upload/complete``: by the time we enqueue,
        the bytes are already assembled in S3 (the browser PUT them
        directly via presigned URLs). This worker job pulls those
        bytes from S3 and writes them into Version Engine, decoupling hash
        latency from the user-visible upload progress.
        """
        redis = await self.get_pool()
        job = await redis.enqueue_job(
            "etl_finalize_upload_job", task_id, _queue_name=self.queue_name
        )
        return job.job_id
