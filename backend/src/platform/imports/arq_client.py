"""ARQ client for durable one-time import jobs."""

from __future__ import annotations

import logging

from arq.connections import ArqRedis, RedisSettings, create_pool

from src.ingest.file.config import etl_config

logger = logging.getLogger(__name__)


class ImportArqClient:
    """Small enqueue helper for one-time import jobs."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        queue_name: str | None = None,
    ):
        self.redis_url = redis_url or etl_config.etl_redis_url
        self.queue_name = queue_name or etl_config.import_arq_queue_name
        self._pool: ArqRedis | None = None

    async def get_pool(self) -> ArqRedis:
        if self._pool is None:
            settings = RedisSettings.from_dsn(self.redis_url)
            self._pool = await create_pool(settings)
            logger.info("ImportArqClient: redis pool created")
        return self._pool

    async def enqueue_import(self, job_id: str) -> str:
        redis = await self.get_pool()
        job = await redis.enqueue_job(
            "execute_import_job",
            job_id,
            _queue_name=self.queue_name,
        )
        return job.job_id
