from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.platform.synchronize.jobs import execute_sync_run


@dataclass
class FakeRun:
    id: str = "run-1"
    connection_id: str = "conn-1"
    status: str = "queued"
    trigger_type: str = "scheduled"


class FakeRunRepository:
    def __init__(self, run: FakeRun | None = None, *, stale: bool = False):
        self.run = run or FakeRun()
        self.stale = stale
        self.completed: list[tuple[str, str, str | None]] = []
        self.failed: list[tuple[str, str, str | None]] = []
        self.marked_stale: list[str] = []
        self.renewed: list[str] = []

    def get_by_id(self, run_id: str):
        return self.run if run_id == self.run.id else None

    def is_stale(self, run: FakeRun, *, lease_seconds: int, **_kwargs):
        assert run is self.run
        assert lease_seconds > 0
        return self.stale

    def mark_stale(self, run_id: str):
        self.marked_stale.append(run_id)
        self.run.status = "failed"
        return self.run

    def claim_running(self, run_id: str, *, lease_seconds: int):
        if run_id != self.run.id or self.run.status != "queued":
            return None
        assert lease_seconds > 0
        self.run.status = "running"
        return self.run

    def renew_lease(self, run_id: str, *, lease_seconds: int):
        assert lease_seconds > 0
        self.renewed.append(run_id)
        return run_id == self.run.id and self.run.status == "running"

    def complete(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: str | None = None,
        **_kwargs,
    ):
        self.completed.append((run_id, status, result_summary))
        if status == "failed":
            self.failed.append((run_id, status, _kwargs.get("error")))
        self.run.status = status


class FakeEngine:
    def __init__(self, result=None, error: Exception | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self.result = result
        self.error = error

    async def execute(self, connection_id: str, trigger_type: str, *, run_id: str):
        self.calls.append((connection_id, trigger_type, run_id))
        if self.error:
            raise self.error
        return self.result


class TerminalMutatingEngine:
    def __init__(self, run_repo: FakeRunRepository, terminal_status: str):
        self.run_repo = run_repo
        self.terminal_status = terminal_status
        self.calls: list[tuple[str, str, str]] = []

    async def execute(self, connection_id: str, trigger_type: str, *, run_id: str):
        self.calls.append((connection_id, trigger_type, run_id))
        self.run_repo.run.status = self.terminal_status
        return None


@pytest.mark.asyncio
async def test_sync_worker_marks_unexecuted_queued_run_skipped():
    run_repo = FakeRunRepository()
    engine = FakeEngine()

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result["status"] == "skipped"
    assert engine.calls == [("conn-1", "scheduled", "run-1")]
    assert run_repo.completed == [("run-1", "skipped", "Sync did not run")]


@pytest.mark.asyncio
async def test_sync_worker_returns_completed_result_from_engine():
    run_repo = FakeRunRepository()
    engine = FakeEngine(result={"path": "/Gmail", "commit_id": "commit-1"})

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result == {
        "status": "completed",
        "run_id": "run-1",
        "connection_id": "conn-1",
        "path": "/Gmail",
        "commit_id": "commit-1",
    }
    assert engine.calls == [("conn-1", "scheduled", "run-1")]
    assert run_repo.completed == []


@pytest.mark.asyncio
async def test_sync_worker_marks_run_failed_when_engine_raises():
    run_repo = FakeRunRepository()
    engine = FakeEngine(error=RuntimeError("provider unavailable"))

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result == {
        "status": "failed",
        "run_id": "run-1",
        "error": "provider unavailable",
    }
    assert engine.calls == [("conn-1", "scheduled", "run-1")]
    assert run_repo.failed == [("run-1", "failed", "provider unavailable")]


@pytest.mark.asyncio
async def test_sync_worker_skips_terminal_run_without_engine_call():
    run_repo = FakeRunRepository(FakeRun(status="failed"))
    engine = FakeEngine()

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result == {
        "status": "skipped",
        "run_id": "run-1",
        "run_status": "failed",
    }
    assert engine.calls == []
    assert run_repo.completed == []


@pytest.mark.asyncio
async def test_sync_worker_skips_run_that_was_already_claimed():
    run_repo = FakeRunRepository(FakeRun(status="running"))
    engine = FakeEngine()

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result == {
        "status": "skipped",
        "run_id": "run-1",
        "run_status": "running",
        "reason": "run_not_claimed",
    }
    assert engine.calls == []
    assert run_repo.completed == []


@pytest.mark.asyncio
async def test_sync_worker_marks_stale_run_failed_without_engine_call():
    run_repo = FakeRunRepository(stale=True)
    engine = FakeEngine()

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result == {
        "status": "failed",
        "run_id": "run-1",
        "run_status": "failed",
        "reason": "run_lease_expired",
    }
    assert engine.calls == []
    assert run_repo.marked_stale == ["run-1"]
    assert run_repo.completed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
async def test_sync_worker_does_not_overwrite_terminal_status_set_by_engine(terminal_status):
    run_repo = FakeRunRepository()
    engine = TerminalMutatingEngine(run_repo, terminal_status)

    result = await execute_sync_run(
        {
            "sync_run_repository": run_repo,
            "synchronize_engine": engine,
        },
        "run-1",
    )

    assert result == {
        "status": terminal_status,
        "run_id": "run-1",
        "connection_id": "conn-1",
    }
    assert engine.calls == [("conn-1", "scheduled", "run-1")]
    assert run_repo.completed == []
