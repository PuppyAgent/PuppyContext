from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.platform.synchronize.router import _queue_sync_run, trigger_pull


class FakeRun:
    id = "run-1"
    status = "queued"
    worker_job_id = None


class FakeRunRepository:
    def __init__(self, active_run: FakeRun | None = None):
        self.active_run = active_run
        self.created: list[tuple[str, str]] = []
        self.worker_job_ids: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str, str | None]] = []
        self.stale_run_ids: set[str] = set()
        self.marked_stale: list[str] = []

    def get_active_by_sync(self, sync_id: str):
        return self.active_run

    def get_blocking_active_by_sync(self, sync_id: str):
        active_run = self.get_active_by_sync(sync_id)
        if active_run and active_run.id in self.stale_run_ids:
            self.mark_stale(active_run.id)
            self.active_run = None
            return None
        return active_run

    def mark_stale(self, run_id: str):
        self.marked_stale.append(run_id)
        return None

    def create_queued(self, sync_id: str, trigger_type: str):
        self.created.append((sync_id, trigger_type))
        return FakeRun()

    def create_queued_single_lane(self, sync_id: str, trigger_type: str):
        if self.active_run:
            return self.active_run, False
        return self.create_queued(sync_id, trigger_type), True

    def set_worker_job_id(self, run_id: str, worker_job_id: str):
        self.worker_job_ids.append((run_id, worker_job_id))

    def complete(self, run_id: str, *, status: str, error: str | None = None, **_kwargs):
        self.completed.append((run_id, status, error))


class RaceRunRepository(FakeRunRepository):
    def get_active_by_sync(self, sync_id: str):
        return None

    def get_blocking_active_by_sync(self, sync_id: str):
        return None

    def create_queued_single_lane(self, sync_id: str, trigger_type: str):
        active_run = FakeRun()
        active_run.id = "run-race-winner"
        active_run.status = "queued"
        active_run.worker_job_id = "arq-race-winner"
        return active_run, False


class FakeSyncArqClient:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.enqueued: list[str] = []

    async def enqueue_sync_run(self, run_id: str):
        self.enqueued.append(run_id)
        if self.error:
            raise self.error
        return "arq-job-1"


def _connection(**overrides):
    data = {
        "id": "conn-1",
        "direction": "inbound",
        "status": "active",
        "path": "/Gmail",
        "provider": "gmail",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_queue_sync_run_creates_queued_run_and_enqueues_worker(monkeypatch):
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient()

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    result = await _queue_sync_run(
        connection=_connection(),
        trigger_type="manual",
        sync_arq_client=arq_client,
    )

    assert result == {
        "connection_id": "conn-1",
        "access_point_id": "conn-1",
        "run_id": "run-1",
        "worker_job_id": "arq-job-1",
        "path": "/Gmail",
        "provider": "gmail",
        "status": "queued",
        "summary": "Sync queued",
        "deduped": False,
    }
    assert run_repo.created == [("conn-1", "manual")]
    assert arq_client.enqueued == ["run-1"]
    assert run_repo.worker_job_ids == [("run-1", "arq-job-1")]
    assert run_repo.completed == []


@pytest.mark.asyncio
async def test_queue_sync_run_rejects_paused_connection_before_creating_run(monkeypatch):
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient()

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    with pytest.raises(HTTPException) as exc:
        await _queue_sync_run(
            connection=_connection(status="paused"),
            trigger_type="manual",
            sync_arq_client=arq_client,
        )

    assert exc.value.status_code == 409
    assert run_repo.created == []
    assert arq_client.enqueued == []


@pytest.mark.asyncio
async def test_queue_sync_run_reuses_existing_active_run(monkeypatch):
    active_run = FakeRun()
    active_run.id = "run-active"
    active_run.status = "running"
    active_run.worker_job_id = "arq-active"
    run_repo = FakeRunRepository(active_run=active_run)
    arq_client = FakeSyncArqClient()

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    result = await _queue_sync_run(
        connection=_connection(status="syncing"),
        trigger_type="manual",
        sync_arq_client=arq_client,
    )

    assert result == {
        "connection_id": "conn-1",
        "access_point_id": "conn-1",
        "run_id": "run-active",
        "worker_job_id": "arq-active",
        "path": "/Gmail",
        "provider": "gmail",
        "status": "running",
        "summary": "Sync already running",
        "deduped": True,
    }
    assert run_repo.created == []
    assert arq_client.enqueued == []


@pytest.mark.asyncio
async def test_queue_sync_run_reuses_active_run_even_when_connection_paused(monkeypatch):
    active_run = FakeRun()
    active_run.id = "run-paused"
    active_run.status = "queued"
    active_run.worker_job_id = "arq-paused"
    run_repo = FakeRunRepository(active_run=active_run)
    arq_client = FakeSyncArqClient()

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    result = await _queue_sync_run(
        connection=_connection(status="paused"),
        trigger_type="manual",
        sync_arq_client=arq_client,
    )

    assert result["run_id"] == "run-paused"
    assert result["worker_job_id"] == "arq-paused"
    assert result["status"] == "queued"
    assert result["deduped"] is True
    assert run_repo.created == []
    assert arq_client.enqueued == []


@pytest.mark.asyncio
async def test_queue_sync_run_recovers_stale_active_run_then_queues(monkeypatch):
    stale_run = FakeRun()
    stale_run.id = "run-stale"
    stale_run.status = "running"
    run_repo = FakeRunRepository(active_run=stale_run)
    run_repo.stale_run_ids.add("run-stale")
    arq_client = FakeSyncArqClient()

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    result = await _queue_sync_run(
        connection=_connection(),
        trigger_type="manual",
        sync_arq_client=arq_client,
    )

    assert result["run_id"] == "run-1"
    assert result["deduped"] is False
    assert run_repo.marked_stale == ["run-stale"]
    assert run_repo.created == [("conn-1", "manual")]
    assert arq_client.enqueued == ["run-1"]


@pytest.mark.asyncio
async def test_queue_sync_run_dedupes_unique_race_without_enqueue(monkeypatch):
    run_repo = RaceRunRepository()
    arq_client = FakeSyncArqClient()

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    result = await _queue_sync_run(
        connection=_connection(),
        trigger_type="manual",
        sync_arq_client=arq_client,
    )

    assert result == {
        "connection_id": "conn-1",
        "access_point_id": "conn-1",
        "run_id": "run-race-winner",
        "worker_job_id": "arq-race-winner",
        "path": "/Gmail",
        "provider": "gmail",
        "status": "queued",
        "summary": "Sync already queued",
        "deduped": True,
    }
    assert run_repo.created == []
    assert arq_client.enqueued == []


@pytest.mark.asyncio
async def test_queue_sync_run_marks_run_failed_when_worker_enqueue_fails(monkeypatch):
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient(error=RuntimeError("redis unavailable"))

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    with pytest.raises(HTTPException) as exc:
        await _queue_sync_run(
            connection=_connection(),
            trigger_type="initial",
            sync_arq_client=arq_client,
        )

    assert exc.value.status_code == 503
    assert run_repo.created == [("conn-1", "initial")]
    assert arq_client.enqueued == ["run-1"]
    assert run_repo.worker_job_ids == []
    assert run_repo.completed == [
        (
            "run-1",
            "failed",
            "Failed to enqueue sync worker: redis unavailable",
        )
    ]


@pytest.mark.asyncio
async def test_trigger_pull_all_returns_existing_running_and_skips_unqueueable(monkeypatch):
    active_run = FakeRun()
    active_run.id = "run-active"
    active_run.status = "running"
    active_run.worker_job_id = "arq-active"

    class PullAllRunRepository(FakeRunRepository):
        def get_active_by_sync(self, sync_id: str):
            return active_run if sync_id == "conn-running" else None

    run_repo = PullAllRunRepository()
    arq_client = FakeSyncArqClient()

    class FakeRepository:
        def list_by_project(self, project_id):
            return [
                _connection(id="conn-running", status="syncing"),
                _connection(id="conn-paused", status="paused"),
                _connection(id="conn-outbound", direction="outbound", status="active"),
            ]

    class FakeAuthorization:
        def authorize(self, project_id, user_id, action):
            return SimpleNamespace(project_id=project_id, user_id=user_id)

    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    response = await trigger_pull(
        connection_id=None,
        project_id="project-1",
        provider=None,
        service=SimpleNamespace(repository=FakeRepository()),
        sync_arq_client=arq_client,
        authorization=FakeAuthorization(),
        current_user=SimpleNamespace(user_id="user-1"),
    )

    assert response.code == 0
    assert response.data is not None
    assert response.data.synced == 1
    assert [item["connection_id"] for item in response.data.results] == ["conn-running"]
    assert all(item["deduped"] is True for item in response.data.results)
    assert arq_client.enqueued == []
