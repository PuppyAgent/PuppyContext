"""HTTP/resource lifecycle regression with real routers, service and policy.

Only storage, user authentication and job transport are isolated doubles. This
is not a claim of real PG/Redis/OAuth or target-environment acceptance.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.synchronize import router
from src.platform.synchronize.public_router import router as public_router
from src.platform.synchronize.dependencies import (
    get_sync_arq_client, get_synchronize_provider_registry, get_synchronize_service,
)
from src.platform.synchronize.models import SynchronizeBinding
from src.platform.synchronize.run_repository import SyncRun
from src.platform.synchronize.service import SynchronizeService
from src.provider.registry import ProviderRegistry
from src.provider.url.adapter import UrlProvider
from tests.authorization_fakes import authorization_for, install_authorization


class BindingStore:
    def __init__(self):
        self.items = {}
        self.counter = 0

    def create(self, **fields):
        self.counter += 1
        binding = SynchronizeBinding(id=f"binding-{self.counter}", **fields)
        self.items[binding.id] = binding
        return binding

    def get_by_id(self, binding_id):
        return self.items.get(binding_id)

    def list_by_project(self, project_id):
        return [b for b in self.items.values() if b.project_id == project_id]

    def update_status(self, binding_id, status):
        self.items[binding_id].status = status

    def delete(self, binding_id):
        del self.items[binding_id]


class RunStore:
    def __init__(self):
        self.items = {}

    def get_blocking_active_by_sync(self, binding_id):
        return next((r for r in self.items.values() if r.synchronize_binding_id == binding_id and r.status == "queued"), None)

    def create_queued_single_lane(self, binding_id, trigger_type):
        run = SyncRun(id=f"run-{len(self.items) + 1}", synchronize_binding_id=binding_id, status="queued", trigger_type=trigger_type)
        self.items[run.id] = run
        return run, True

    def set_worker_job_id(self, run_id, worker_job_id):
        self.items[run_id].worker_job_id = worker_job_id

    def list_by_sync(self, binding_id, **_kwargs):
        return [r for r in self.items.values() if r.synchronize_binding_id == binding_id]

    def get_by_id(self, run_id):
        return self.items.get(run_id)

    def complete(self, run_id, **fields):
        for key, value in fields.items():
            setattr(self.items[run_id], key, value)


@pytest.fixture
def environment(monkeypatch):
    from src.infra.scheduler import service as scheduler

    monkeypatch.setattr(scheduler, "get_scheduler_service", lambda: SimpleNamespace(sync_trigger=AsyncMock()))
    bindings = BindingStore()
    runs = RunStore()
    monkeypatch.setattr(router, "_get_run_repo", lambda: runs)
    registry = ProviderRegistry()
    registry.register(UrlProvider())
    service = SynchronizeService(bindings)
    service.register_provider(registry.get("url"))
    queue = SimpleNamespace(enqueue_sync_run=AsyncMock(return_value="worker-job"))
    app = FastAPI()
    app.include_router(public_router, prefix="/api/v1")
    app.dependency_overrides[get_synchronize_service] = lambda: service
    app.dependency_overrides[get_synchronize_provider_registry] = lambda: registry
    app.dependency_overrides[get_sync_arq_client] = lambda: queue
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="user-1", role="authenticated")
    install_authorization(app, authorization_for("project-1", role="admin"))
    return app, bindings, runs, queue


def create(client, project="project-1"):
    return client.post("/api/v1/synchronize/bindings", json={
        "project_id": project, "provider": "url", "target_path": "folder-without-scope",
        "config": {"source": {"resource_url": "https://example.test", "resource_name": "Same name"}, "options": {}},
        "sync_mode": "manual", "trigger": {"type": "manual"},
    })


def test_create_reload_failed_retry_pause_resume_and_delete_keep_one_binding_identity(environment):
    app, bindings, runs, queue = environment
    # A second same-name resource must not be merged into the first one.
    with TestClient(app) as client:
        first = create(client)
        assert first.status_code == 200, first.text
        first_id = first.json()["data"]["binding"]["id"]
        second_id = create(client).json()["data"]["binding"]["id"]
        assert first_id != second_id
    # New client session: list is read from the same persistent resource store,
    # not the create response or any Access/Scope inventory.
    with TestClient(app) as client:
        rows = client.get("/api/v1/synchronize/bindings?project_id=project-1").json()["data"]
        assert {b["id"] for b in rows} == {first_id, second_id}
        assert all(b["trigger"] == {"type": "manual"} for b in rows)
        runs.items["run-1"].status = "failed"
        refresh = client.post(f"/api/v1/synchronize/bindings/{first_id}/refresh")
        assert refresh.status_code == 200
        assert refresh.json()["data"]["results"][0]["synchronize_binding_id"] == first_id
        history = client.get(f"/api/v1/synchronize/bindings/{first_id}/runs").json()["data"]
        assert {r["id"] for r in history} == {"run-1", "run-3"}
        assert all(r["synchronize_binding_id"] == first_id for r in history)
        runs.items["run-3"].status = "failed"
        assert client.post(f"/api/v1/synchronize/bindings/{first_id}/pause").status_code == 200
        before = queue.enqueue_sync_run.call_count
        assert client.post(f"/api/v1/synchronize/bindings/{first_id}/refresh").status_code == 409
        assert queue.enqueue_sync_run.call_count == before
        assert client.post(f"/api/v1/synchronize/bindings/{first_id}/resume").status_code == 200
        assert bindings.items[second_id].status == "active"
        assert client.delete(f"/api/v1/synchronize/bindings/{first_id}").status_code == 200
        assert set(bindings.items) == {second_id}


@pytest.mark.parametrize("resource_id", ["access-id", "missing", "foreign-binding"])
@pytest.mark.parametrize("operation,method", [("pause", "post"), ("refresh", "post"), ("resume", "post"), ("", "delete"), ("runs", "get")])
def test_wrong_resource_ids_or_foreign_projects_cannot_mutate_bindings(environment, resource_id, operation, method):
    app, bindings, _, queue = environment
    bindings.items["foreign-binding"] = SynchronizeBinding(id="foreign-binding", project_id="other-project", provider="url")
    with TestClient(app) as client:
        path = f"/api/v1/synchronize/bindings/{resource_id}" + (f"/{operation}" if operation else "")
        response = getattr(client, method)(path)
    assert response.status_code in {403, 404}, response.text
    assert bindings.items["foreign-binding"].status == "active"
    queue.enqueue_sync_run.assert_not_called()


def test_viewer_can_read_but_cannot_create_or_pause(environment):
    app, bindings, _, queue = environment
    binding = bindings.create(project_id="project-1", provider="url", path="")
    install_authorization(app, authorization_for("project-1", role="viewer"))
    with TestClient(app) as client:
        assert client.get("/api/v1/synchronize/bindings?project_id=project-1").status_code == 200
        assert create(client).status_code == 403
        assert client.post(f"/api/v1/synchronize/bindings/{binding.id}/pause").status_code == 403
    assert binding.status == "active"
    queue.enqueue_sync_run.assert_not_called()
