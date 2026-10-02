"""ARQ client for GitHub work on the Synchronize-owned logical queue."""

from __future__ import annotations

import logging

from arq.connections import ArqRedis, RedisSettings, create_pool

from src.platform.synchronize.config import synchronize_config

logger = logging.getLogger(__name__)


class GithubSyncArqClient:
    """GitHub synchronization producer; preserves webhook deduplication identities."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        queue_name: str | None = None,
    ):
        self.redis_url = redis_url or synchronize_config.redis_url
        self.queue_name = queue_name or synchronize_config.synchronize_arq_queue_name
        self._pool: ArqRedis | None = None

    async def get_pool(self) -> ArqRedis:
        if self._pool is None:
            settings = RedisSettings.from_dsn(self.redis_url)
            self._pool = await create_pool(settings)
            logger.info("GithubSyncArqClient: redis pool created")
        return self._pool

    async def enqueue_pull(
        self,
        synchronize_github_binding_id: str,
        *,
        branch: str | None = None,
        force: bool = False,
        triggered_by: str = "webhook",
        dedup_key: str | None = None,
    ) -> str | None:
        """Enqueue a GitHub pull onto the Synchronize worker queue.

        ``dedup_key`` (e.g. ``gh-import:<integration>:<sha>``) is passed as the
        ARQ ``_job_id`` so a redelivered webhook for the same push does not
        double-run. ARQ returns ``None`` when a job with that id already exists,
        which we surface as ``None`` (treated as "already queued").
        """
        redis = await self.get_pool()
        job = await redis.enqueue_job(
            "execute_synchronize_github_pull",
            synchronize_github_binding_id,
            branch=branch,
            force=force,
            triggered_by=triggered_by,
            _queue_name=self.queue_name,
            _job_id=dedup_key,
        )
        return job.job_id if job is not None else None


_client: GithubSyncArqClient | None = None

def get_github_sync_arq_client() -> GithubSyncArqClient:
    global _client
    if _client is None:
        _client = GithubSyncArqClient()
    return _client
