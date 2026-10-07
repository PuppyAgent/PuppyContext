from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.infra.scheduler.jobs.sync_job import _execute_sync_pull_async
from src.platform.synchronize.router import _queue_sync_run


class FakeRun:
    id = "run-1"
    status = "queued"
    worker_job_id = None


class FakeConnection:
    def __init__(self, sync_id: str, *, direction: str = "inbound", status: str = "active"):
        self.id = sync_id
        self.direction = direction
        self.status = status


class FakeIntegrationRepository:
    connection = FakeConnection("conn-1")

    def __init__(self, _supabase):
        pass

    def get_by_id(self, sync_id: str):
        if self.connection is None:
            return None
        self.connection.id = sync_id
        return self.connection


class FakeRunRepository:
    active_run: FakeRun | None = None

    def __init__(self):
        self.active_run = type(self).active_run
        self.created: list[tuple[str, str]] = []
        self.worker_job_ids: list[tuple[str, str]] = []
        self.stale_run_ids: set[str] = set()
        self.marked_stale: list[str] = []

    def get_active_by_sync(self, sync_id: str):
        return self.active_run

    def get_blocking_active_by_sync(self, sync_id: str):
        if self.active_run and self.active_run.id in self.stale_run_ids:
            self.mark_stale(self.active_run.id)
            self.active_run = None
        return self.active_run

    def mark_stale(self, run_id: str):
        self.marked_stale.append(run_id)
        return None

    def create_queued(self, sync_id: str, trigger_type: str = "manual"):
        self.created.append((sync_id, trigger_type))
        run = FakeRun()
        self.active_run = run
        return run

    def create_queued_single_lane(self, sync_id: str, trigger_type: str = "manual"):
        if self.active_run:
            return self.active_run, False
        return self.create_queued(sync_id, trigger_type), True

    def set_worker_job_id(self, run_id: str, worker_job_id: str):
        self.worker_job_ids.append((run_id, worker_job_id))
        if self.active_run and self.active_run.id == run_id:
            self.active_run.worker_job_id = worker_job_id


class FakeSyncArqClient:
    def __init__(self):
        self.enqueued: list[str] = []

    async def enqueue_sync_run(self, run_id: str):
        self.enqueued.append(run_id)
        return "arq-job-1"


@pytest.mark.asyncio
async def test_scheduled_sync_job_queues_scheduled_run(monkeypatch):
    FakeRunRepository.active_run = None
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient()
    FakeIntegrationRepository.connection = FakeConnection(
        "conn-1",
        direction="inbound",
        status="active",
    )

    import src.platform.synchronize.run_repository as run_repo_module
    import src.infra.supabase.client as supabase_module
    import src.platform.synchronize.arq_client as arq_module
    import src.platform.synchronize.repository as integration_repo_module

    monkeypatch.setattr(supabase_module, "SupabaseClient", lambda: object())
    monkeypatch.setattr(
        integration_repo_module,
        "SynchronizeRepository",
        FakeIntegrationRepository,
    )
    monkeypatch.setattr(run_repo_module, "SyncRunRepository", lambda _supabase: run_repo)
    monkeypatch.setattr(arq_module, "SyncArqClient", lambda: arq_client)

    result = await _execute_sync_pull_async("conn-1")

    assert result["status"] == "queued"
    assert result["run_id"] == "run-1"
    assert run_repo.created == [("conn-1", "scheduled")]
    assert arq_client.enqueued == ["run-1"]
    assert run_repo.worker_job_ids == [("run-1", "arq-job-1")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "reason"),
    [
        (
            FakeConnection("conn-1", direction="outbound", status="active"),
            "not_configured_for_inbound_sync",
        ),
        (
            FakeConnection("conn-1", direction="inbound", status="paused"),
            "status_paused",
        ),
    ],
)
async def test_scheduled_sync_job_skips_unqueueable_connections(
    monkeypatch,
    connection,
    reason,
):
    FakeRunRepository.active_run = None
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient()
    FakeIntegrationRepository.connection = connection

    import src.platform.synchronize.run_repository as run_repo_module
    import src.infra.supabase.client as supabase_module
    import src.platform.synchronize.arq_client as arq_module
    import src.platform.synchronize.repository as integration_repo_module

    monkeypatch.setattr(supabase_module, "SupabaseClient", lambda: object())
    monkeypatch.setattr(
        integration_repo_module,
        "SynchronizeRepository",
        FakeIntegrationRepository,
    )
    monkeypatch.setattr(run_repo_module, "SyncRunRepository", lambda _supabase: run_repo)
    monkeypatch.setattr(arq_module, "SyncArqClient", lambda: arq_client)

    result = await _execute_sync_pull_async("conn-1")

    assert result == {
        "status": "skipped",
        "access_point_id": "conn-1",
        "reason": reason,
    }
    assert run_repo.created == []
    assert arq_client.enqueued == []


@pytest.mark.asyncio
async def test_scheduled_sync_job_reuses_active_run(monkeypatch):
    active_run = FakeRun()
    active_run.id = "run-active"
    active_run.status = "running"
    active_run.worker_job_id = "arq-active"
    FakeRunRepository.active_run = active_run
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient()
    FakeIntegrationRepository.connection = FakeConnection(
        "conn-1",
        direction="inbound",
        status="syncing",
    )

    import src.platform.synchronize.run_repository as run_repo_module
    import src.infra.supabase.client as supabase_module
    import src.platform.synchronize.arq_client as arq_module
    import src.platform.synchronize.repository as integration_repo_module

    monkeypatch.setattr(supabase_module, "SupabaseClient", lambda: object())
    monkeypatch.setattr(
        integration_repo_module,
        "SynchronizeRepository",
        FakeIntegrationRepository,
    )
    monkeypatch.setattr(run_repo_module, "SyncRunRepository", lambda _supabase: run_repo)
    monkeypatch.setattr(arq_module, "SyncArqClient", lambda: arq_client)

    result = await _execute_sync_pull_async("conn-1")

    assert result == {
        "status": "running",
        "access_point_id": "conn-1",
        "connection_id": "conn-1",
        "run_id": "run-active",
        "worker_job_id": "arq-active",
        "deduped": True,
        "reason": "sync_already_running",
    }
    assert run_repo.created == []
    assert arq_client.enqueued == []
    FakeRunRepository.active_run = None


@pytest.mark.asyncio
async def test_scheduled_sync_job_recovers_stale_active_run(monkeypatch):
    active_run = FakeRun()
    active_run.id = "run-stale"
    active_run.status = "running"
    FakeRunRepository.active_run = active_run
    run_repo = FakeRunRepository()
    run_repo.stale_run_ids.add("run-stale")
    arq_client = FakeSyncArqClient()
    FakeIntegrationRepository.connection = FakeConnection(
        "conn-1",
        direction="inbound",
        status="active",
    )

    import src.platform.synchronize.run_repository as run_repo_module
    import src.infra.supabase.client as supabase_module
    import src.platform.synchronize.arq_client as arq_module
    import src.platform.synchronize.repository as integration_repo_module

    monkeypatch.setattr(supabase_module, "SupabaseClient", lambda: object())
    monkeypatch.setattr(
        integration_repo_module,
        "SynchronizeRepository",
        FakeIntegrationRepository,
    )
    monkeypatch.setattr(run_repo_module, "SyncRunRepository", lambda _supabase: run_repo)
    monkeypatch.setattr(arq_module, "SyncArqClient", lambda: arq_client)

    result = await _execute_sync_pull_async("conn-1")

    assert result["status"] == "queued"
    assert result["run_id"] == "run-1"
    assert run_repo.marked_stale == ["run-stale"]
    assert run_repo.created == [("conn-1", "scheduled")]
    assert arq_client.enqueued == ["run-1"]
    FakeRunRepository.active_run = None


@pytest.mark.asyncio
async def test_scheduled_then_manual_refresh_reuses_same_active_run(monkeypatch):
    FakeRunRepository.active_run = None
    run_repo = FakeRunRepository()
    arq_client = FakeSyncArqClient()
    FakeIntegrationRepository.connection = FakeConnection(
        "conn-1",
        direction="inbound",
        status="active",
    )

    import src.platform.synchronize.run_repository as run_repo_module
    import src.infra.supabase.client as supabase_module
    import src.platform.synchronize.arq_client as arq_module
    import src.platform.synchronize.repository as integration_repo_module
    import src.platform.synchronize.router as router_module

    monkeypatch.setattr(supabase_module, "SupabaseClient", lambda: object())
    monkeypatch.setattr(
        integration_repo_module,
        "SynchronizeRepository",
        FakeIntegrationRepository,
    )
    monkeypatch.setattr(run_repo_module, "SyncRunRepository", lambda _supabase: run_repo)
    monkeypatch.setattr(arq_module, "SyncArqClient", lambda: arq_client)
    monkeypatch.setattr(router_module, "_get_run_repo", lambda: run_repo)

    scheduled = await _execute_sync_pull_async("conn-1")
    manual = await _queue_sync_run(
        connection=SimpleNamespace(
            id="conn-1",
            direction="inbound",
            status="active",
            path="/Gmail",
            provider="gmail",
        ),
        trigger_type="manual",
        sync_arq_client=arq_client,
    )

    assert scheduled["run_id"] == "run-1"
    assert scheduled["worker_job_id"] == "arq-job-1"
    assert manual == {
        "synchronize_binding_id": "conn-1",
        "synchronize_run_id": "run-1",
        "worker_job_id": "arq-job-1",
        "path": "/Gmail",
        "provider": "gmail",
        "status": "queued",
        "summary": "Sync already queued",
        "deduped": True,
    }
    assert run_repo.created == [("conn-1", "scheduled")]
    assert arq_client.enqueued == ["run-1"]
    FakeRunRepository.active_run = None
