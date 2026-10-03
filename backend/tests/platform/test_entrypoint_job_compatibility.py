"""Post-drain task contract: no Synchronize jobs remain on Import workers."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arq.jobs import deserialize_job, serialize_job


@pytest.mark.parametrize("attempt", [1, 3])
@pytest.mark.parametrize("keyword_id", [False, True])
async def test_serialized_github_pull_is_consumed_only_by_synchronize(monkeypatch, attempt, keyword_id):
    from src.platform.synchronize.github import jobs
    from src.platform.imports.worker import WorkerSettings as ImportWorker
    from src.platform.synchronize.worker import WorkerSettings

    repository = SimpleNamespace(get_by_id=AsyncMock(return_value={"id": "binding-1"}))
    pull = AsyncMock(return_value=SimpleNamespace(status="success", git_sha="sha"))
    monkeypatch.setattr(jobs, "GithubSyncRepository", lambda: repository)
    monkeypatch.setattr(jobs, "import_branch", pull)
    kwargs = {"branch": "main", "force": True, "triggered_by": "webhook"}
    args = () if keyword_id else ("binding-1",)
    if keyword_id:
        kwargs["synchronize_github_binding_id"] = "binding-1"
    job = deserialize_job(serialize_job("execute_synchronize_github_pull", args, kwargs, attempt, 1750000000000))
    consumers = {fn.__name__: fn for fn in WorkerSettings.functions}
    result = await consumers[job.function]({}, *job.args, **job.kwargs)
    pull.assert_awaited_once_with({"id": "binding-1"}, branch="main", force=True, triggered_by="webhook")
    assert result["status"] == "success"
    assert result["synchronize_github_binding_id"] == "binding-1"
    assert "integration_id" not in result
    assert job.job_try == attempt
    assert {fn.__name__ for fn in ImportWorker.functions} == {"execute_import_job"}
    assert not {"execute_github_import", "execute_github_sync_pull", "execute_sync_run"} & consumers.keys()


async def test_github_producer_uses_synchronize_queue_and_preserves_deduplication_key():
    from src.platform.synchronize.github.arq_client import GithubSyncArqClient
    from src.platform.synchronize.worker import WorkerSettings
    from src.platform.imports.worker import WorkerSettings as ImportWorker

    client = GithubSyncArqClient()
    redis = SimpleNamespace(enqueue_job=AsyncMock(return_value=SimpleNamespace(job_id="job-1")))
    client._pool = redis
    assert await client.enqueue_pull("binding-1", branch="main", dedup_key="gh-import:binding-1:sha") == "job-1"
    redis.enqueue_job.assert_awaited_once_with(
        "execute_synchronize_github_pull", "binding-1", branch="main", force=False, triggered_by="webhook",
        _queue_name=WorkerSettings.queue_name, _job_id="gh-import:binding-1:sha",
    )
    assert WorkerSettings.queue_name != ImportWorker.queue_name
    redis.enqueue_job.return_value = None
    assert await client.enqueue_pull("binding-1", dedup_key="gh-import:binding-1:sha") is None


async def test_api_and_worker_wiring_imports_and_startup(monkeypatch):
    from src.main import app
    from src.platform.imports import worker as imports
    from src.platform.synchronize import worker as synchronize
    from src.provider.registry import ProviderRegistry

    assert app.openapi()["paths"]
    monkeypatch.setattr(imports, "ImportJobRepository", lambda: object())
    monkeypatch.setattr(imports, "OneTimeImportRunner", lambda: object())
    monkeypatch.setattr(synchronize, "get_synchronize_provider_registry", ProviderRegistry)
    monkeypatch.setattr(synchronize, "SupabaseClient", lambda: object())
    monkeypatch.setattr(synchronize, "SyncRunRepository", lambda _: object())
    monkeypatch.setattr(synchronize, "SynchronizeRepository", lambda _: object())
    import_context, sync_context = {}, {}
    await imports.startup(import_context)
    await synchronize.startup(sync_context)
    assert "one_time_import_runner" in import_context
    assert "synchronize_engine" in sync_context
    assert {fn.__name__ for fn in synchronize.WorkerSettings.functions} == {
        "execute_synchronize_run", "execute_synchronize_github_pull",
    }
    await imports.shutdown(import_context)
    await synchronize.shutdown(sync_context)
