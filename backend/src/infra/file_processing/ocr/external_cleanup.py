"""Strict cleanup port for Project-owned external OCR resources.

The Project deletion worker persists a snapshot before relational teardown,
then retries ``cleanup`` with that same snapshot until it returns COMPLETE.
Provider cancellation, local MineRU cache deletion, and Redis state deletion
are deliberately separate and verified operations.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from src.infra.file_processing.mineru.config import mineru_config
from src.infra.file_processing.ocr.base import (
    OCRProvider,
    OCRProviderCleanupResult,
    OCRProviderCleanupState,
)

_SAFE_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


async def _await_cleanup_boundary(awaitable):
    """Wait for cleanup to really finish before propagating cancellation."""

    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.result()
        raise cancelled


class ProjectETLTaskSource(Protocol):
    def list_tasks(
        self,
        project_id: str | None = None,
        status: Any | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]: ...


@dataclass(frozen=True, order=True)
class ExternalProviderHandle:
    provider: str
    task_id: str
    requires_cancellation: bool = True
    source_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExternalIngestCleanupSnapshot:
    project_id: str
    provider_handles: tuple[ExternalProviderHandle, ...] = ()
    redis_keys: tuple[str, ...] = ()
    cache_task_ids: tuple[str, ...] = ()
    etl_task_ids: tuple[str, ...] = ()
    arq_job_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExternalIngestCleanupSnapshot:
        return cls(
            project_id=str(value["project_id"]),
            provider_handles=tuple(
                ExternalProviderHandle(
                    provider=str(item["provider"]),
                    task_id=str(item["task_id"]),
                    requires_cancellation=bool(item.get("requires_cancellation", True)),
                    source_task_ids=tuple(
                        str(task_id) for task_id in item.get("source_task_ids", ())
                    ),
                )
                for item in value.get("provider_handles", ())
            ),
            redis_keys=tuple(str(item) for item in value.get("redis_keys", ())),
            cache_task_ids=tuple(str(item) for item in value.get("cache_task_ids", ())),
            etl_task_ids=tuple(str(item) for item in value.get("etl_task_ids", ())),
            arq_job_ids=tuple(str(item) for item in value.get("arq_job_ids", ())),
            errors=tuple(str(item) for item in value.get("errors", ())),
        )


@dataclass(frozen=True)
class ExternalIngestCleanupResult:
    state: OCRProviderCleanupState
    provider_results: tuple[OCRProviderCleanupResult, ...]
    deleted_redis_keys: tuple[str, ...]
    deleted_cache_task_ids: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.state == OCRProviderCleanupState.COMPLETE


class MineRUCacheCleanup:
    """Delete exact MineRU task directories without following traversal."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root if root is not None else mineru_config.mineru_cache_dir
        self.root = Path(configured).expanduser().resolve()

    def task_path(self, task_id: str) -> Path:
        normalized = normalize_opaque_id(task_id, label="MineRU cache task ID")
        candidate = self.root / normalized
        if candidate.parent != self.root:
            raise ValueError("MineRU cache task path escaped cache root")
        return candidate

    def delete(self, task_id: str) -> bool:
        path = self.task_path(task_id)
        if path.is_symlink():
            path.unlink()
            return True
        if not path.exists():
            return False
        if not path.is_dir():
            path.unlink()
            return True
        shutil.rmtree(path, ignore_errors=False)
        return True

    def exists(self, task_id: str) -> bool:
        path = self.task_path(task_id)
        return path.exists() or path.is_symlink()


def normalize_opaque_id(value: Any, *, label: str) -> str:
    """Normalize provider/task IDs that later become local path components."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip()
    if not _SAFE_OPAQUE_ID.fullmatch(normalized) or normalized in {".", ".."}:
        raise ValueError(f"{label} is not a safe opaque ID")
    return normalized


def normalize_provider(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("OCR provider must be a string")
    normalized = value.strip().lower()
    if not _SAFE_PROVIDER.fullmatch(normalized):
        raise ValueError("OCR provider is invalid")
    return normalized


class ExternalIngestCleanup:
    """Snapshot, delete, and verify Project-owned OCR side effects."""

    def __init__(
        self,
        *,
        task_source: ProjectETLTaskSource,
        redis: Any,
        providers: dict[str, OCRProvider] | None = None,
        cache: MineRUCacheCleanup | None = None,
        redis_prefix: str,
        default_provider: str | None = None,
        page_size: int = 100,
    ) -> None:
        self.task_source = task_source
        self.redis = redis
        self.providers = {
            normalize_provider(name): provider for name, provider in (providers or {}).items()
        }
        self.cache = cache or MineRUCacheCleanup()
        if not isinstance(redis_prefix, str) or not redis_prefix:
            raise ValueError("The lifecycle owner must supply its Redis state namespace")
        self.redis_prefix = f"{redis_prefix}:" if not redis_prefix.endswith(":") else redis_prefix
        self.redis_task_prefix = f"{self.redis_prefix}task:"
        self.default_provider = normalize_provider(default_provider) if default_provider else None
        self.page_size = max(1, min(page_size, 1000))

    async def snapshot(self, project_id: str) -> ExternalIngestCleanupSnapshot:
        """Traverse SQL tasks plus Redis to capture all durable cleanup handles."""

        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        project_id = project_id.strip()
        tasks = await self._list_all_tasks(project_id)

        handles: dict[tuple[str, str], ExternalProviderHandle] = {}
        redis_keys: set[str] = set()
        cache_task_ids: set[str] = set()
        etl_task_ids: set[str] = set()
        arq_job_ids: set[str] = set()
        errors: list[str] = []

        for task in tasks:
            task_id = str(getattr(task, "task_id", "") or "").strip()
            if not task_id:
                errors.append("ETL task without task_id encountered during snapshot")
                continue
            etl_task_ids.add(task_id)
            redis_keys.add(self._redis_key(task_id))
            metadata = self._task_metadata(task)
            self._collect_task_handles(
                task,
                task_id=task_id,
                metadata=metadata,
                handles=handles,
                cache_task_ids=cache_task_ids,
                errors=errors,
            )

        async for raw_key in self.redis.scan_iter(match=f"{self.redis_task_prefix}*"):
            key = self._decode_text(raw_key)
            try:
                raw = await self.redis.get(raw_key)
            except Exception as exc:
                errors.append(f"failed to read Redis state {key}: {exc}")
                continue
            state = self._decode_state(raw)
            if state is None or str(state.get("project_id") or "") != project_id:
                continue

            redis_keys.add(key)
            state_task_id = str(state.get("task_id") or "").strip()
            if state_task_id:
                etl_task_ids.add(state_task_id)
            metadata = state.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            provider = state.get("provider_name") or self._provider_from(metadata)
            provider_task_id = state.get("provider_task_id")
            if provider_task_id:
                terminal = state.get("provider_task_terminal")
                external = state.get("provider_task_external")
                requires_cancellation = not (
                    external is False
                    or terminal is True
                    or state.get("artifact_mineru_markdown_key")
                )
                # Old state was only assigned after parse_document completed.
                if external is None:
                    requires_cancellation = False
                self._add_handle(
                    handles,
                    provider=provider,
                    provider_task_id=provider_task_id,
                    task_id=state_task_id,
                    requires_cancellation=requires_cancellation,
                    cache_task_ids=cache_task_ids,
                    errors=errors,
                )
            for field in ("arq_job_id_ocr", "arq_job_id_postprocess"):
                job_id = state.get(field)
                if isinstance(job_id, str) and job_id:
                    arq_job_ids.add(job_id)

        return ExternalIngestCleanupSnapshot(
            project_id=project_id,
            provider_handles=tuple(sorted(handles.values())),
            redis_keys=tuple(sorted(redis_keys)),
            cache_task_ids=tuple(sorted(cache_task_ids)),
            etl_task_ids=tuple(sorted(etl_task_ids)),
            arq_job_ids=tuple(sorted(arq_job_ids)),
            errors=tuple(errors),
        )

    async def cleanup(
        self,
        snapshot: ExternalIngestCleanupSnapshot,
    ) -> ExternalIngestCleanupResult:
        """Cancel providers, delete local/Redis state, then verify absence."""

        provider_results: list[OCRProviderCleanupResult] = []
        errors = list(snapshot.errors)
        handles, redis_keys, cache_task_ids = self._validated_resources(
            snapshot,
            errors,
        )

        for handle in handles:
            provider_results.append(await self._cleanup_provider(handle))

        deleted_cache_ids: list[str] = []
        for task_id in cache_task_ids:
            try:
                if await _await_cleanup_boundary(
                    asyncio.to_thread(self.cache.delete, task_id)
                ):
                    deleted_cache_ids.append(task_id)
            except Exception as exc:
                errors.append(f"failed to delete MineRU cache {task_id}: {exc}")

        deleted_redis_keys: list[str] = []
        for key in redis_keys:
            try:
                removed = await self.redis.delete(key)
                if int(removed or 0) > 0:
                    deleted_redis_keys.append(key)
            except Exception as exc:
                errors.append(f"failed to delete Redis state {key}: {exc}")

        for task_id in cache_task_ids:
            try:
                if await _await_cleanup_boundary(
                    asyncio.to_thread(self.cache.exists, task_id)
                ):
                    errors.append(f"MineRU cache still exists for {task_id}")
            except Exception as exc:
                errors.append(f"failed to verify MineRU cache {task_id}: {exc}")

        for key in redis_keys:
            try:
                if bool(await self.redis.exists(key)):
                    errors.append(f"Redis state still exists: {key}")
            except Exception as exc:
                errors.append(f"failed to verify Redis state {key}: {exc}")

        state = self._aggregate_state(provider_results, errors)
        return ExternalIngestCleanupResult(
            state=state,
            provider_results=tuple(provider_results),
            deleted_redis_keys=tuple(deleted_redis_keys),
            deleted_cache_task_ids=tuple(deleted_cache_ids),
            errors=tuple(errors),
        )

    def _validated_resources(
        self,
        snapshot: ExternalIngestCleanupSnapshot,
        errors: list[str],
    ) -> tuple[
        tuple[ExternalProviderHandle, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        handles: list[ExternalProviderHandle] = []
        for handle in snapshot.provider_handles:
            try:
                provider = normalize_provider(handle.provider)
                task_id = normalize_opaque_id(
                    handle.task_id,
                    label=f"{provider} provider task ID",
                )
            except ValueError as exc:
                errors.append(f"invalid provider cleanup handle: {exc}")
                continue
            if provider != handle.provider or task_id != handle.task_id:
                errors.append("provider cleanup handle was not normalized")
                continue
            handles.append(handle)

        redis_keys: list[str] = []
        for key in snapshot.redis_keys:
            if not key.startswith(self.redis_task_prefix):
                errors.append(f"refusing non-ETL Redis cleanup key: {key}")
                continue
            try:
                task_id = normalize_opaque_id(
                    key.removeprefix(self.redis_task_prefix),
                    label="Redis ETL task ID",
                )
            except ValueError as exc:
                errors.append(f"refusing invalid Redis cleanup key {key}: {exc}")
                continue
            if key != self._redis_key(task_id):
                errors.append(f"refusing non-canonical Redis cleanup key: {key}")
                continue
            redis_keys.append(key)

        cache_task_ids: list[str] = []
        for task_id in snapshot.cache_task_ids:
            try:
                normalized = normalize_opaque_id(
                    task_id,
                    label="MineRU cache task ID",
                )
            except ValueError as exc:
                errors.append(f"refusing invalid MineRU cache handle: {exc}")
                continue
            if normalized != task_id:
                errors.append("MineRU cache handle was not normalized")
                continue
            cache_task_ids.append(task_id)

        return tuple(handles), tuple(redis_keys), tuple(cache_task_ids)

    async def _list_all_tasks(self, project_id: str) -> list[Any]:
        tasks: list[Any] = []
        offset = 0
        list_page = getattr(
            self.task_source,
            "list_tasks_strict",
            self.task_source.list_tasks,
        )
        while True:
            page = list_page(
                project_id=project_id,
                limit=self.page_size,
                offset=offset,
            )
            if inspect.isawaitable(page):
                page = await page
            if not isinstance(page, list):
                raise RuntimeError("ETL task source returned an invalid page")
            tasks.extend(page)
            if len(page) < self.page_size:
                return tasks
            offset += len(page)

    def _collect_task_handles(
        self,
        task: Any,
        *,
        task_id: str,
        metadata: dict[str, Any],
        handles: dict[tuple[str, str], ExternalProviderHandle],
        cache_task_ids: set[str],
        errors: list[str],
    ) -> None:
        provider = self._provider_from(metadata)
        provider_task_id = metadata.get("provider_task_id")
        if provider_task_id:
            external = metadata.get("provider_task_external")
            terminal = metadata.get("provider_task_terminal")
            # Legacy jobs only persisted this field after provider completion.
            requires_cancellation = external is True and terminal is not True
            self._add_handle(
                handles,
                provider=provider,
                provider_task_id=provider_task_id,
                task_id=task_id,
                requires_cancellation=requires_cancellation,
                cache_task_ids=cache_task_ids,
                errors=errors,
            )

        mineru_task_id = metadata.get("mineru_task_id")
        result = getattr(task, "result", None)
        if result is not None:
            mineru_task_id = getattr(result, "mineru_task_id", None) or mineru_task_id
        if mineru_task_id:
            self._add_handle(
                handles,
                provider="mineru",
                provider_task_id=mineru_task_id,
                task_id=task_id,
                requires_cancellation=False,
                cache_task_ids=cache_task_ids,
                errors=errors,
            )

    def _add_handle(
        self,
        handles: dict[tuple[str, str], ExternalProviderHandle],
        *,
        provider: Any,
        provider_task_id: Any,
        task_id: str,
        requires_cancellation: bool,
        cache_task_ids: set[str],
        errors: list[str],
    ) -> None:
        try:
            normalized_provider = normalize_provider(provider or "unknown")
            normalized_id = normalize_opaque_id(
                provider_task_id,
                label=f"{normalized_provider} provider task ID",
            )
        except ValueError as exc:
            errors.append(f"task {task_id or '(unknown)'}: {exc}")
            return

        key = (normalized_provider, normalized_id)
        previous = handles.get(key)
        sources = set(previous.source_task_ids if previous else ())
        if task_id:
            sources.add(task_id)
        # Any durable terminal evidence is enough to avoid cancelling a job
        # that the provider already reported complete.
        should_cancel = requires_cancellation and (
            previous.requires_cancellation if previous else True
        )
        handles[key] = ExternalProviderHandle(
            provider=normalized_provider,
            task_id=normalized_id,
            requires_cancellation=should_cancel,
            source_task_ids=tuple(sorted(sources)),
        )
        if normalized_provider == "mineru":
            cache_task_ids.add(normalized_id)

    async def _cleanup_provider(
        self,
        handle: ExternalProviderHandle,
    ) -> OCRProviderCleanupResult:
        if not handle.requires_cancellation:
            return OCRProviderCleanupResult(
                provider=handle.provider,
                task_id=handle.task_id,
                state=OCRProviderCleanupState.COMPLETE,
                detail="durable ETL state proves provider job is terminal",
            )

        provider = self.providers.get(handle.provider)
        if provider is None:
            return OCRProviderCleanupResult(
                provider=handle.provider,
                task_id=handle.task_id,
                state=OCRProviderCleanupState.UNSUPPORTED,
                detail=f"no cancellation adapter registered for {handle.provider}",
            )
        try:
            result = await _await_cleanup_boundary(
                provider.cancel_external_job(handle.task_id)
            )
        except Exception as exc:
            return OCRProviderCleanupResult(
                provider=handle.provider,
                task_id=handle.task_id,
                state=OCRProviderCleanupState.FAILED,
                detail=str(exc),
                retryable=True,
            )
        if result.provider != handle.provider or result.task_id != handle.task_id:
            return OCRProviderCleanupResult(
                provider=handle.provider,
                task_id=handle.task_id,
                state=OCRProviderCleanupState.FAILED,
                detail="provider cancellation result did not match requested handle",
                retryable=True,
            )
        return result

    @staticmethod
    def _aggregate_state(
        provider_results: list[OCRProviderCleanupResult],
        errors: list[str],
    ) -> OCRProviderCleanupState:
        if errors or any(
            result.state == OCRProviderCleanupState.FAILED for result in provider_results
        ):
            return OCRProviderCleanupState.FAILED
        if any(result.state == OCRProviderCleanupState.PENDING for result in provider_results):
            return OCRProviderCleanupState.PENDING
        if any(result.state == OCRProviderCleanupState.UNSUPPORTED for result in provider_results):
            return OCRProviderCleanupState.UNSUPPORTED
        return OCRProviderCleanupState.COMPLETE

    def _redis_key(self, task_id: str) -> str:
        return f"{self.redis_task_prefix}{task_id}"

    def _provider_from(self, metadata: dict[str, Any]) -> str:
        value = metadata.get("ocr_provider") or metadata.get("provider")
        if value:
            try:
                return normalize_provider(value)
            except ValueError:
                return "unknown"
        return self.default_provider or "unknown"

    @staticmethod
    def _task_metadata(task: Any) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        config = getattr(task, "config", None)
        if isinstance(config, dict):
            config_metadata = config.get("metadata")
            if isinstance(config_metadata, dict):
                merged.update(config_metadata)
            for key in (
                "ocr_provider",
                "provider_task_id",
                "provider_task_external",
                "provider_task_terminal",
                "mineru_task_id",
            ):
                if key in config:
                    merged[key] = config[key]
        metadata = getattr(task, "metadata", None)
        if isinstance(metadata, dict):
            merged.update(metadata)
        return merged

    @staticmethod
    def _decode_text(value: Any) -> str:
        if isinstance(value, bytes | bytearray):
            return value.decode("utf-8")
        return str(value)

    @classmethod
    def _decode_state(cls, raw: Any) -> dict[str, Any] | None:
        if raw is None:
            return None
        try:
            decoded = cls._decode_text(raw)
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None
