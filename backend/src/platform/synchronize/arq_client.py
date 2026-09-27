"""ARQ client for durable Integration sync runs."""

from __future__ import annotations

import logging

from arq.connections import ArqRedis, RedisSettings, create_pool

from src.ingest.file.config import etl_config

logger = logging.getLogger(__name__)


class SyncArqClient:
    """Small enqueue helper for connection sync runs."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        queue_name: str | None = None,
    ):
        self.redis_url = redis_url or etl_config.etl_redis_url
        self.queue_name = queue_name or etl_config.sync_arq_queue_name
        self._pool: ArqRedis | None = None

    async def get_pool(self) -> ArqRedis:
        if self._pool is None:
            settings = RedisSettings.from_dsn(self.redis_url)
            self._pool = await create_pool(settings)
            logger.info("SyncArqClient: redis pool created")
        return self._pool

    async def enqueue_sync_run(self, run_id: str) -> str:
        redis = await self.get_pool()
        job = await redis.enqueue_job(
            "execute_sync_run",
            run_id,
            _queue_name=self.queue_name,
        )
        if job is None:
            raise RuntimeError(f"Sync run {run_id} was not enqueued")
        return job.job_id
