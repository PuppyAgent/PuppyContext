"""
ETL Runtime State Repository (Redis)

Store runtime state as JSON in Redis.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from arq.connections import ArqRedis

from src.platform.upload.config import upload_config
from src.platform.upload.state.models import ETLRuntimeState

logger = logging.getLogger(__name__)


class ETLStateRepositoryRedis:
    def __init__(
        self,
        redis: ArqRedis,
        *,
        key_prefix: str | None = None,
        ttl_seconds: int | None = None,
        terminal_ttl_seconds: int | None = None,
    ):
        self.redis = redis
        self.key_prefix = (
            key_prefix if key_prefix is not None else upload_config.etl_redis_prefix
        )
        self.ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else upload_config.etl_state_ttl_seconds
        )
        self.terminal_ttl_seconds = (
            terminal_ttl_seconds
            if terminal_ttl_seconds is not None
            else upload_config.etl_state_terminal_ttl_seconds
        )

    def _key(self, task_id: str | int) -> str:
        prefix = self.key_prefix
        if prefix and not prefix.endswith(":"):
            prefix = f"{prefix}:"
        return f"{prefix}task:{task_id}"

    async def get(self, task_id: str | int) -> ETLRuntimeState | None:
        raw = await self.redis.get(self._key(task_id))
        if not raw:
            return None
        try:
            if isinstance(raw, bytes | bytearray):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            return ETLRuntimeState.model_validate(data)
        except Exception as e:
            logger.warning(f"Failed to decode runtime state for task_id={task_id}: {e}")
            return None

    async def set(
        self, state: ETLRuntimeState, *, ttl_seconds: int | None = None
    ) -> None:
        state.updated_at = datetime.now(UTC)
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        # Redis SET only accepts bytes/str/int/float. Store JSON string for compatibility.
        await self.redis.set(self._key(state.task_id), state.model_dump_json(), ex=ttl)

    async def merge(
        self, task_id: str | int, patch: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> ETLRuntimeState | None:
        current = await self.get(task_id)
        if current is None:
            return None
        data = current.model_dump()
        data.update(patch)
        data["updated_at"] = datetime.now(UTC)
        next_state = ETLRuntimeState.model_validate(data)
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        await self.redis.set(self._key(task_id), next_state.model_dump_json(), ex=ttl)
        return next_state

    async def set_terminal(self, state: ETLRuntimeState) -> None:
        """Mark terminal state and shorten TTL."""
        await self.set(state, ttl_seconds=self.terminal_ttl_seconds)
