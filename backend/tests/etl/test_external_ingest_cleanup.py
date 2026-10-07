from __future__ import annotations

import asyncio
import fnmatch
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from src.infra.file_processing.ocr.base import (
    OCRExternalJob,
    OCRExternalJobCompletion,
    OCRProvider,
    OCRProviderCleanupResult,
    OCRProviderCleanupState,
    ParsedDocument,
    parse_document_with_external_lifecycle,
)
from src.infra.file_processing.ocr.external_cleanup import (
    ExternalIngestCleanup,
    ExternalIngestCleanupSnapshot,
    ExternalProviderHandle,
    MineRUCacheCleanup,
)
from src.infra.file_processing.ocr.lifecycle import run_ocr_lifecycle_under_project_lease
from src.platform.upload.tasks.models import ETLTask


class FakeTaskSource:
    def __init__(self, tasks: list[ETLTask]) -> None:
        self.tasks = tasks
        self.offsets: list[int] = []

    def list_tasks(
        self,
        project_id: str | None = None,
        status: Any | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ETLTask]:
        self.offsets.append(offset)
        matching = [task for task in self.tasks if task.project_id == project_id]
        return matching[offset : offset + limit]


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    async def scan_iter(self, *, match: str):
        for key in sorted(self.values):
            if fnmatch.fnmatch(key, match):
                yield key.encode()

    async def get(self, key: str | bytes) -> str | None:
        if isinstance(key, bytes):
            key = key.decode()
        return self.values.get(key)

    async def delete(self, key: str | bytes) -> int:
        if isinstance(key, bytes):
            key = key.decode()
        return 1 if self.values.pop(key, None) is not None else 0

    async def exists(self, key: str | bytes) -> int:
        if isinstance(key, bytes):
            key = key.decode()
        return int(key in self.values)


class FakeProvider(OCRProvider):
    def __init__(
        self,
        name: str,
        cancellations: list[OCRProviderCleanupResult | Exception] | None = None,
    ) -> None:
        self._name = name
        self.cancellations = list(cancellations or [])
        self.cancel_calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def parse_document(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> ParsedDocument:
        return ParsedDocument(task_id=data_id or "inline", markdown_content="ok")

    async def health_check(self) -> bool:
        return True

    async def cancel_external_job(self, task_id: str) -> OCRProviderCleanupResult:
        self.cancel_calls.append(task_id)
        if not self.cancellations:
            return await super().cancel_external_job(task_id)
        outcome = self.cancellations.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def task_with_handle(
    task_id: str,
    *,
    project_id: str = "project-a",
    provider: str = "mineru",
    provider_task_id: str = "provider-1",
    external: bool | None = True,
    terminal: bool | None = False,
) -> ETLTask:
    metadata: dict[str, Any] = {
        "ocr_provider": provider,
        "provider_task_id": provider_task_id,
    }
    if external is not None:
        metadata["provider_task_external"] = external
    if terminal is not None:
        metadata["provider_task_terminal"] = terminal
    return ETLTask(
        task_id=task_id,
        project_id=project_id,
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_snapshot_traverses_all_task_pages_and_orphan_redis(tmp_path: Path) -> None:
    tasks = [
        task_with_handle(f"task-{index}", provider_task_id=f"remote-{index}") for index in range(3)
    ]
    source = FakeTaskSource(tasks)
    redis = FakeRedis(
        {
            "etl:task:orphan": json.dumps(
                {
                    "task_id": "orphan",
                    "project_id": "project-a",
                    "provider_name": "reducto",
                    "provider_task_id": "reducto-orphan",
                    "provider_task_external": True,
                    "provider_task_terminal": False,
                    "arq_job_id_ocr": "arq-orphan",
                    "metadata": {},
                }
            ),
            "etl:task:other-project": json.dumps(
                {"task_id": "other-project", "project_id": "project-b"}
            ),
        }
    )
    cleanup = ExternalIngestCleanup(
        task_source=source,
        redis=redis,
        cache=MineRUCacheCleanup(tmp_path / ".mineru_cache"),
        redis_prefix="etl:",
        page_size=2,
    )

    snapshot = await cleanup.snapshot("project-a")

    assert source.offsets == [0, 2]
    assert snapshot.etl_task_ids == ("orphan", "task-0", "task-1", "task-2")
    assert snapshot.arq_job_ids == ("arq-orphan",)
    assert {handle.task_id for handle in snapshot.provider_handles} == {
        "remote-0",
        "remote-1",
        "remote-2",
        "reducto-orphan",
    }
    assert "etl:task:other-project" not in snapshot.redis_keys


@pytest.mark.asyncio
async def test_traversal_handle_is_rejected_without_deleting_outside_cache(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / ".mineru_cache"
    cache_root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    source = FakeTaskSource([task_with_handle("task-1", provider_task_id="../victim")])
    cleanup = ExternalIngestCleanup(
        task_source=source,
        redis_prefix="etl:",
        redis=FakeRedis(),
        cache=MineRUCacheCleanup(cache_root),
    )

    snapshot = await cleanup.snapshot("project-a")
    result = await cleanup.cleanup(snapshot)

    assert snapshot.cache_task_ids == ()
    assert snapshot.errors
    assert result.state == OCRProviderCleanupState.FAILED
    assert (victim / "keep.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_replayed_snapshot_cannot_delete_unscoped_redis_or_cache(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / ".mineru_cache"
    victim = tmp_path / "victim"
    victim.mkdir()
    redis = FakeRedis({"unscoped:key": "keep"})
    cleanup = ExternalIngestCleanup(
        task_source=FakeTaskSource([]),
        redis_prefix="etl:",
        redis=redis,
        cache=MineRUCacheCleanup(cache_root),
    )
    snapshot = ExternalIngestCleanupSnapshot(
        project_id="project-a",
        provider_handles=(ExternalProviderHandle(provider="mineru", task_id="../victim"),),
        redis_keys=("unscoped:key",),
        cache_task_ids=("../victim",),
    )

    result = await cleanup.cleanup(snapshot)

    assert result.state == OCRProviderCleanupState.FAILED
    assert redis.values == {"unscoped:key": "keep"}
    assert victim.exists()


@pytest.mark.asyncio
async def test_cleanup_deletes_and_verifies_redis_and_cache_idempotently(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / ".mineru_cache"
    task_cache = cache_root / "provider-1"
    task_cache.mkdir(parents=True)
    (task_cache / "full.md").write_text("cached")
    task = task_with_handle(
        "task-1",
        external=None,
        terminal=None,
    )
    redis = FakeRedis(
        {
            "etl:task:task-1": json.dumps(
                {
                    "task_id": "task-1",
                    "project_id": "project-a",
                    "provider_name": "mineru",
                    "provider_task_id": "provider-1",
                    "metadata": {},
                }
            )
        }
    )
    cleanup = ExternalIngestCleanup(
        task_source=FakeTaskSource([task]),
        redis_prefix="etl:",
        redis=redis,
        cache=MineRUCacheCleanup(cache_root),
    )
    snapshot = await cleanup.snapshot("project-a")

    first = await cleanup.cleanup(snapshot)
    second = await cleanup.cleanup(snapshot)

    assert first.state == OCRProviderCleanupState.COMPLETE
    assert first.deleted_cache_task_ids == ("provider-1",)
    assert first.deleted_redis_keys == ("etl:task:task-1",)
    assert second.state == OCRProviderCleanupState.COMPLETE
    assert second.deleted_cache_task_ids == ()
    assert second.deleted_redis_keys == ()
    assert not task_cache.exists()
    assert redis.values == {}


@pytest.mark.asyncio
async def test_provider_failure_can_retry_same_durable_snapshot(tmp_path: Path) -> None:
    failed = RuntimeError("provider unavailable")
    complete = OCRProviderCleanupResult(
        provider="reducto",
        task_id="remote-1",
        state=OCRProviderCleanupState.COMPLETE,
    )
    provider = FakeProvider("reducto", [failed, complete])
    redis = FakeRedis({"etl:task:task-1": "{}"})
    cleanup = ExternalIngestCleanup(
        redis_prefix="etl:",
        task_source=FakeTaskSource(
            [
                task_with_handle(
                    "task-1",
                    provider="reducto",
                    provider_task_id="remote-1",
                )
            ]
        ),
        redis=redis,
        providers={"reducto": provider},
        cache=MineRUCacheCleanup(tmp_path / ".mineru_cache"),
    )
    snapshot = await cleanup.snapshot("project-a")

    first = await cleanup.cleanup(snapshot)
    second = await cleanup.cleanup(snapshot)

    assert first.state == OCRProviderCleanupState.FAILED
    assert first.provider_results[0].retryable is True
    assert second.state == OCRProviderCleanupState.COMPLETE
    assert provider.cancel_calls == ["remote-1", "remote-1"]


@pytest.mark.asyncio
async def test_provider_without_cancel_api_is_not_reported_complete(
    tmp_path: Path,
) -> None:
    provider = FakeProvider("mineru")
    cleanup = ExternalIngestCleanup(
        task_source=FakeTaskSource([task_with_handle("task-1")]),
        redis_prefix="etl:",
        redis=FakeRedis(),
        providers={"mineru": provider},
        cache=MineRUCacheCleanup(tmp_path / ".mineru_cache"),
    )

    result = await cleanup.cleanup(await cleanup.snapshot("project-a"))

    assert result.state == OCRProviderCleanupState.UNSUPPORTED
    assert result.complete is False
    assert result.provider_results[0].state == OCRProviderCleanupState.UNSUPPORTED


class SplitLifecycleProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__("split")
        self.allow_completion = asyncio.Event()

    async def create_external_job(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> OCRExternalJob:
        return OCRExternalJob(provider=self.name, task_id="remote-split")

    async def wait_external_job(
        self,
        job: OCRExternalJob,
    ) -> OCRExternalJobCompletion:
        await self.allow_completion.wait()
        return OCRExternalJobCompletion(job=job, metadata={"markdown": "done"})

    async def materialize_external_job(
        self,
        completion: OCRExternalJobCompletion,
    ) -> ParsedDocument:
        return ParsedDocument(
            task_id=completion.job.task_id,
            markdown_content=str(completion.metadata["markdown"]),
        )


@pytest.mark.asyncio
async def test_external_handle_hook_runs_before_provider_wait() -> None:
    provider = SplitLifecycleProvider()
    persisted = asyncio.Event()
    handles: list[OCRExternalJob] = []

    async def persist(handle: OCRExternalJob) -> None:
        handles.append(handle)
        persisted.set()

    operation = asyncio.create_task(
        parse_document_with_external_lifecycle(
            provider,
            file_url="https://example.test/file.pdf",
            data_id="task-1",
            on_created=persist,
        )
    )
    await asyncio.wait_for(persisted.wait(), timeout=1)

    assert handles[0].task_id == "remote-split"
    assert not operation.done()
    provider.allow_completion.set()
    parsed = await operation
    assert parsed.markdown_content == "done"


@pytest.mark.asyncio
async def test_thread_cancellation_does_not_release_project_lease_early() -> None:
    thread_started = threading.Event()
    release_thread = threading.Event()
    thread_finished = threading.Event()
    lease_exited = asyncio.Event()
    exit_saw_finished: list[bool] = []

    class ThreadProvider(FakeProvider):
        async def parse_document(
            self,
            file_url: str,
            data_id: str | None = None,
        ) -> ParsedDocument:
            def blocking() -> ParsedDocument:
                thread_started.set()
                release_thread.wait(timeout=5)
                thread_finished.set()
                return ParsedDocument(task_id="thread", markdown_content="done")

            return await asyncio.to_thread(blocking)

    class Lease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> bool:
            exit_saw_finished.append(thread_finished.is_set())
            lease_exited.set()
            return False

    def lease_factory(project_id: str, operation: str, **kwargs: Any) -> Lease:
        return Lease()

    operation = asyncio.create_task(
        run_ocr_lifecycle_under_project_lease(
            lease_factory=lease_factory,
            project_id="project-a",
            provider=ThreadProvider("thread"),
            file_url="https://example.test/file.pdf",
        )
    )
    assert await asyncio.to_thread(thread_started.wait, 1)

    operation.cancel()
    await asyncio.sleep(0)
    assert not lease_exited.is_set()

    release_thread.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert lease_exited.is_set()
    assert exit_saw_finished == [True]
