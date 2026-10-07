import asyncio

import pytest

from src.exceptions import AppException, ErrorCode
from src.platform.synchronize.github import exporter

INTEGRATION = {
    "id": "integration-1",
    "project_id": "project-1",
    "github_repo_owner": "owner",
    "github_repo_name": "repo",
    "default_branch": "main",
    "oauth_connection_id": "oauth-1",
}


class _FakeApi:
    def __init__(self, _token: str):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _BlockingLease:
    def __init__(self):
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *_args):
        self.exited.set()
        return False


def _prepare_export(monkeypatch) -> _FakeApi:
    api = _FakeApi("token")

    async def load_oauth(_oauth_id: str):
        return {"access_token": "token", "user_id": "initiator"}

    monkeypatch.setattr(
        "src.platform.synchronize.github.importer._load_oauth_token",
        load_oauth,
    )
    monkeypatch.setattr(exporter, "GithubApi", lambda _token: api)
    monkeypatch.setattr(exporter, "GithubSyncLogRepository", object)
    monkeypatch.setattr(exporter, "GithubSyncRepository", object)
    return api


@pytest.mark.asyncio
async def test_cancellation_waits_for_remote_export_before_releasing_lease(monkeypatch):
    api = _prepare_export(monkeypatch)
    remote_started = asyncio.Event()
    finish_remote = asyncio.Event()
    lease = _BlockingLease()
    lease_calls: list[tuple[str, str]] = []

    async def blocked_export(**_kwargs):
        remote_started.set()
        await finish_remote.wait()
        return object()

    def lease_factory(project_id: str, operation: str):
        lease_calls.append((project_id, operation))
        return lease

    monkeypatch.setattr(exporter, "_do_export", blocked_export)
    task = asyncio.create_task(
        exporter.export_to_branch(
            INTEGRATION,
            write_lease_factory=lease_factory,
        )
    )
    await asyncio.wait_for(remote_started.wait(), timeout=2)

    task.cancel()
    await asyncio.sleep(0)

    assert lease_calls == [("project-1", "github.export")]
    assert lease.entered.is_set()
    assert not lease.exited.is_set()
    assert not task.done()

    finish_remote.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.exited.is_set()
    assert api.closed is True


@pytest.mark.asyncio
async def test_deleting_project_rejects_new_export_before_project_read(monkeypatch):
    api = _prepare_export(monkeypatch)
    export_called = False

    async def should_not_export(**_kwargs):
        nonlocal export_called
        export_called = True
        return object()

    class _RejectingLease:
        async def __aenter__(self):
            raise AppException(
                code=ErrorCode.VERSION_CONFLICT,
                status_code=409,
                message="Project is not accepting writes",
                details={"code": "project_write_admission_closed"},
            )

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(exporter, "_do_export", should_not_export)

    with pytest.raises(AppException) as exc_info:
        await exporter.export_to_branch(
            {**INTEGRATION, "oauth_connection_id": None},
            write_lease_factory=lambda _project_id, _operation: _RejectingLease(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"code": "project_write_admission_closed"}
    assert export_called is False
    assert api.closed is False
