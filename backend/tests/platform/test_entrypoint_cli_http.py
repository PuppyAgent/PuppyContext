"""Real CLI process -> loopback HTTP -> real routers/services/authorization.

Only external storage, queue transport, entitlement facts and provider IO are
substituted. This does not certify Supabase migration or deployment readiness.
"""
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI

from src.platform.access import project_router as project_access
from src.platform.access import public_router as public_access
from src.platform.access import router as access
from src.platform.access.model_repository import AccessModelRepository
from src.platform.access.service import AccessService
from src.platform.access.surface_repository import _row_to_surface
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.imports import router as imports
from src.platform.imports.repository import ImportJob
from src.platform.imports.service import ImportJobService
from src.platform.synchronize import public_router as public_synchronize
from src.platform.synchronize import router as synchronize
from src.platform.synchronize.models import SynchronizeBinding
from src.platform.synchronize.run_repository import SyncRun
from src.platform.synchronize.service import SynchronizeService
from tests.authorization_fakes import authorization_for, install_authorization
from tests.platform.test_entrypoint_admission import Adapter, catalog

pytestmark = pytest.mark.integration
CLI = Path(__file__).resolve().parents[3] / "cli/bin/puppyone.js"


class ImportStore:
    def __init__(self):
        self.rows = {}

    def create(self, **values):
        job = ImportJob(id=f"job-{len(self.rows) + 1}",
                        created_at="2026-10-02T12:00:00+00:00",
                        updated_at="2026-10-02T12:00:00+00:00", **values)
        self.rows[job.id] = job
        return job

    def get(self, job_id):
        return self.rows.get(job_id)

    def get_by_idempotency_key(self, **values):
        return next((job for job in self.rows.values()
                     if all(getattr(job, key) == value for key, value in values.items())), None)

    def update(self, job_id, **values):
        job = self.rows[job_id]
        for key, value in values.items():
            setattr(job, key, value)
        return job

    def mark_failed(self, job_id, error):
        return self.update(job_id, status="failed", error_message=error)

    def mark_cancelled(self, job_id):
        return self.update(job_id, status="cancelled")


class BindingStore:
    def __init__(self):
        self.rows = {}

    def create(self, **values):
        row = SynchronizeBinding(id=f"binding-{len(self.rows) + 1}", **values)
        self.rows[row.id] = row
        return row

    def get_by_id(self, key):
        return self.rows.get(key)

    def delete(self, key):
        return self.rows.pop(key, None)


class RunStore:
    def __init__(self):
        self.rows = {}

    def get_by_id(self, key):
        return self.rows.get(key)

    def list_by_sync(self, key, limit=20, offset=0):
        return [row for row in self.rows.values() if row.synchronize_binding_id == key][offset:offset + limit]

    def get_blocking_active_by_sync(self, key):
        return next((row for row in self.rows.values()
                     if row.synchronize_binding_id == key and row.status == "queued"), None)

    def create_queued_single_lane(self, key, **values):
        row = SyncRun(id=f"run-{len(self.rows) + 1}", synchronize_binding_id=key, status="queued", **values)
        self.rows[row.id] = row
        return row, True

    def set_worker_job_id(self, key, value):
        self.rows[key].worker_job_id = value

    def complete(self, key, **values):
        for name, value in values.items():
            setattr(self.rows[key], name, value)


class Queue:
    def __init__(self):
        self.ids = []
        self.available = True

    async def enqueue_import(self, key):
        if not self.available:
            raise ConnectionError("test queue unavailable")
        self.ids.append(key)
        return f"worker-{key}"

    enqueue_sync_run = enqueue_import


class SurfaceStore:
    """Raw storage/issuance boundary; real domain mapping/service remain in use."""
    def __init__(self):
        self.rows = {}
        self.issued = []
        self.sequence = 0

    def get(self, key):
        return self.rows.get(key)

    def get_surface(self, key):
        row = self.get(key)
        return _row_to_surface(row) if row else None

    def list_by_project(self, project, kind=None):
        return self.list_by_projects([project], kind=kind)

    def list_by_projects(self, projects, kind=None, status=None):
        return [row for row in self.rows.values() if row["project_id"] in projects
                and (kind is None or row["kind"] == kind)
                and (status is None or row["status"] == status)]

    def scope_rows_for(self, rows):
        return {"scope-1": {"id": "scope-1", "project_id": "project-1", "path": "notes"}}

    def count_user_surfaces_by_project(self, project):
        return len(self.list_by_project(project))

    def create(self, project_id, name, path=None, kind="sandbox", **config):
        self.sequence += 1
        row = {"id": f"surface-{self.sequence}", "status": "active", "kind": kind,
               "project_id": project_id, "org_id": "org-1", "name": name,
               "scope_id": "scope-1" if path else None, "config": config,
               "created_at": "2026-10-03T00:00:00+00:00", "updated_at": "2026-10-03T00:00:00+00:00"}
        self.rows[row["id"]] = row
        return row

    def update(self, key, patch):
        self.rows[key].update(patch)
        return self.rows[key]

    def delete(self, key):
        return self.rows.pop(key, None) is not None

    def issue(self, key):
        self.issued.append(key)
        return f"test-issued-once-{len(self.issued)}"

    def regenerate_access_key(self, key):
        return {"access_key": self.issue(key)}

    def regenerate_api_key(self, key):
        return {"api_key": self.issue(key)}


@pytest.fixture
def server(monkeypatch, tmp_path):
    if not (CLI.parent.parent / "node_modules/commander").exists():
        pytest.fail("Install CLI dependencies with npm ci before running the HTTP contract gate")
    state = SimpleNamespace(imports=ImportStore(), bindings=BindingStore(), runs=RunStore(),
                            surfaces=SurfaceStore(), queue=Queue())
    registry = catalog(Adapter())
    authorization = authorization_for("project-1", role="admin")
    state.import_service = ImportJobService(repo=state.imports, arq_client=state.queue,
                                            authorization=authorization, registry=registry)
    sync_service = SynchronizeService(state.bindings)
    sync_service.register_provider(registry.get("url"))
    app = FastAPI()
    app.include_router(imports.router, prefix="/api/v1")
    # No legacy /integrations or /access routes: a fallback cannot pass this gate.
    app.include_router(public_synchronize.router, prefix="/api/v1")
    app.include_router(public_access.router, prefix="/api/v1")
    state.requests = []

    @app.middleware("http")
    async def record_request(request, call_next):
        state.requests.append((request.method, request.url.path,
                               request.headers.get("X-PuppyOne-Repository-Contract")))
        return await call_next(request)

    def authorize(role):
        authorization = authorization_for("project-1", role=role)
        state.import_service.authorization = authorization
        install_authorization(app, authorization)

    state.authorize = authorize
    install_authorization(app, authorization)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="user-1", role="authenticated")
    app.dependency_overrides[imports.get_import_job_service] = lambda: state.import_service
    app.dependency_overrides[imports.get_import_provider_registry] = lambda: registry
    app.dependency_overrides[synchronize.get_synchronize_service] = lambda: sync_service
    app.dependency_overrides[synchronize.get_synchronize_provider_registry] = lambda: registry
    app.dependency_overrides[synchronize.get_sync_arq_client] = lambda: state.queue
    app.dependency_overrides[access.get_entitlement_service] = lambda: SimpleNamespace(
        require_allowed=lambda *a: None, require_feature=lambda *a: None,
        require_capacity=lambda *a, **kw: None,
    )
    monkeypatch.setattr(synchronize, "_get_run_repo", lambda: state.runs)
    monkeypatch.setattr(access, "AccessSurfaceRepository", lambda *a: state.surfaces)
    monkeypatch.setattr("src.platform.access.model_repository.AccessSurfaceRepository", lambda *a: state.surfaces)
    access_service = AccessService(repository=AccessModelRepository(), surface_repository=state.surfaces,
                                   scope_repository=SimpleNamespace())
    app.dependency_overrides[project_access.get_access_service] = lambda: access_service
    monkeypatch.setattr(access, "_get_client", object)
    monkeypatch.setattr(access, "resolve_org_ids", lambda *a, **kw: ["org-1"])
    monkeypatch.setattr(access, "_get_user_project_ids", lambda sb, org, auth, user:
                        auth.accessible_project_ids(["project-1", "project-2"], user))
    monkeypatch.setattr("src.repo.access_credentials.AccessCredentialRepository", lambda *a: SimpleNamespace(
        list_active_by_surface=lambda ids: {key: {"key_last4": "TEST"} for key in ids if key in state.surfaces.issued},
    ))

    def create_mcp(**values):
        values.pop("api_key", None)
        row = state.surfaces.create(kind="mcp", **values)
        return {**row, "api_key": state.surfaces.issue(row["id"])}

    monkeypatch.setattr("src.platform.access.adapters.mcp_endpoint.repository.McpEndpointRepository",
                        lambda *a: SimpleNamespace(create=create_mcp, regenerate_api_key=state.surfaces.regenerate_api_key))
    monkeypatch.setattr("src.platform.project.repository.ProjectRepositorySupabase",
                        lambda: SimpleNamespace(get_by_id=lambda _: SimpleNamespace(org_id="org-1")))
    monkeypatch.setattr("src.platform.access.adapters.sandbox_endpoint.repository.SandboxEndpointRepository",
                        lambda *a: state.surfaces)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    http = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=http.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not http.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert http.started, "loopback test server did not start"

        def cli(*args, ok=True):
            result = subprocess.run(["node", str(CLI), "--json", "--api-url", f"http://127.0.0.1:{port}",
                                     "--api-key", "test-token", "--project", "project-1", *args],
                                    capture_output=True, text=True, timeout=15,
                                    env={**os.environ, "HOME": str(tmp_path)})
            assert result.returncode == (0 if ok else 1), result.stdout + result.stderr
            payload = json.loads((result.stdout if ok else result.stderr).strip())
            assert payload["success"] is ok
            return payload

        state.cli = cli
        state.app = app
        yield state
    finally:
        http.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_actual_cli_dispatch_keeps_snapshot_binding_and_surface_ownership(server):
    result = server.cli("access", "add", "url", "https://example.com", "--folder", "/snapshot",
                        "--idempotency-key", "request-1")
    job = server.imports.rows[result["job"]["id"]]
    assert job.target_path == "/snapshot"
    assert job.source_url == "https://example.com"
    assert not server.bindings.rows and not server.surfaces.rows
    retried = server.cli("import", "create", "https://example.com", "--provider", "url",
                         "--idempotency-key", "request-1")
    assert retried["job"]["id"] == job.id
    assert server.queue.ids == [job.id]
    assert server.cli("import", "cancel", job.id)["job"]["status"] == "cancelled"
    assert server.cli("import", "info", job.id)["job"]["status"] == "cancelled"

    created = server.cli("synchronize", "add", "url", "https://example.com", "--folder", "/continuous")
    assert created["binding"]["id"].startswith("binding-")
    assert "sync" not in created
    execution = created["execution_result"]
    assert execution["synchronize_binding_id"] == created["binding"]["id"]
    run_id = execution["synchronize_run_id"]
    assert server.cli("synchronize", "run", run_id)["run"]["synchronize_binding_id"] == created["binding"]["id"]
    assert len(server.cli("synchronize", "runs", created["binding"]["id"])["runs"]) == 1
    assert len(server.bindings.rows) == len(server.runs.rows) == 1
    binding = next(iter(server.bindings.rows.values()))
    assert binding.trigger == {"type": "manual"}
    assert next(iter(server.runs.rows.values())).synchronize_binding_id == binding.id
    # Real Access router and Sandbox service; only their repositories are substituted.
    surface = server.cli("access", "add", "sandbox", "My sandbox")
    assert surface["access"]["id"].startswith("surface-")
    assert surface["access"]["kind"] == "sandbox"
    assert "provider" not in surface["access"]
    server.cli("synchronize", "refresh", surface["access"]["id"], ok=False)
    assert len(server.queue.ids) == 2, "an Access surface ID cannot enqueue a source run"
    assert len(server.surfaces.rows) == len(server.bindings.rows) == len(server.imports.rows) == 1
    assert {item["provider"] for item in server.cli("import", "providers")["providers"]} == {"url", "notion"}


def test_actual_cli_denied_and_queue_failure_do_not_cross_lifecycles(server):
    rejected = server.cli("import", "create", "https://example.com", "--provider", "not-admitted", ok=False)
    assert "not approved for Import" in rejected["error"]["message"]
    server.cli("synchronize", "add", "url", "https://example.com", "--folder", "/bad-schedule",
               "--mode", "scheduled", "--schedule", "a b c d e", ok=False)
    assert not server.bindings.rows
    assert not server.imports.rows and not server.queue.ids
    server.import_service.authorization = authorization_for("project-1", role="viewer")
    server.cli("import", "create", "https://example.com", ok=False)
    assert not server.imports.rows
    server.import_service.authorization = authorization_for("project-1", role="admin")
    server.queue.available = False
    server.cli("import", "create", "https://example.com", "--idempotency-key", "outage-1", ok=False)
    assert next(iter(server.imports.rows.values())).status == "failed"
    # Reuse of an idempotency key exposes the recorded failure; no implicit retry/Sync binding.
    recorded = server.cli("import", "create", "https://example.com", "--idempotency-key", "outage-1")
    assert recorded["job"]["status"] == "failed"
    assert len(server.imports.rows) == 1
    assert not server.bindings.rows and not server.surfaces.rows


def test_actual_cli_access_lifecycle_uses_canonical_identity_and_one_time_credentials(server):
    assert server.cli("access", "providers")["kinds"] == ["agent", "mcp", "sandbox"]
    assert server.cli("access", "schema", "mcp")["surface_type"]["kind"] == "mcp"
    created = server.cli("access", "add", "mcp", "Notes", "--scope", "/notes")
    surface_id = created["access"]["id"]
    first_key = created["access"]["mcp_api_key"]
    assert first_key.startswith("test-issued-once-")
    detail = server.cli("access", "info", surface_id)["access"]
    assert detail["target"] == {"kind": "scope", "project_id": "project-1", "scope_id": "scope-1"}
    assert detail["kind"] == "mcp" and "provider" not in detail
    assert first_key not in json.dumps(detail) and "mcp_api_key" not in detail
    assert server.cli("access", "ls", "--kind", "mcp")["access"][0]["id"] == surface_id
    assert server.cli("access", "ls", "--provider", "sandbox")["access"] == []
    assert server.cli("access", "pause", surface_id)["access"]["status"] == "paused"
    assert server.cli("access", "resume", surface_id)["access"]["status"] == "active"
    server.cli("access", "update", surface_id, "--set", "description=changed")
    assert server.surfaces.rows[surface_id]["config"]["description"] == "changed"
    assert "accesses" in server.surfaces.rows[surface_id]["config"], "partial metadata patch preserves existing fields"
    request_count = len(server.requests)
    server.cli("access", "key", surface_id, ok=False)
    assert len(server.requests) == request_count and len(server.surfaces.issued) == 1
    issued = server.cli("access", "key", surface_id, "--regenerate")["credential"]
    assert issued["access_surface_id"] == surface_id
    assert issued["credential"] != first_key
    assert "access_key" not in issued
    assert issued["credential"] not in json.dumps(server.cli("access", "info", surface_id))
    server.cli("access", "rm", surface_id)
    assert surface_id not in server.surfaces.rows
    missing = server.cli("access", "info", surface_id, ok=False)
    assert missing["error"]["code"] != "SERVER_UPGRADE_REQUIRED"
    assert not server.bindings.rows and not server.runs.rows and not server.imports.rows
    assert all(path.startswith("/api/v1/access/surfaces") and version == "2"
               for _, path, version in server.requests)


def test_actual_cli_access_permissions_and_invalid_metadata_prevent_writes(server):
    created = server.cli("access", "add", "sandbox", "Sandbox")["access"]
    surface_id = created["id"]
    other = server.surfaces.create(project_id="project-2", name="Other tenant")
    before = json.dumps(server.surfaces.rows, sort_keys=True)
    server.authorize("viewer")
    assert server.cli("access", "info", surface_id)["access"]["id"] == surface_id
    assert len(server.cli("access", "ls")["access"]) == 1
    for args in [("add", "mcp", "denied"), ("pause", surface_id), ("rm", surface_id),
                 ("key", surface_id, "--regenerate"), ("update", surface_id, "--set", "description=denied"),
                 ("info", other["id"])]:
        server.cli("access", *args, ok=False)
    server.authorize("admin")
    for args in [("pause", other["id"]), ("rm", other["id"]), ("key", other["id"], "--regenerate"),
                 ("update", surface_id, "--set", "credentials.token=forbidden")]:
        server.cli("access", *args, ok=False)
    assert json.dumps(server.surfaces.rows, sort_keys=True) == before
    assert not server.surfaces.issued and not server.queue.ids


def test_actual_cli_old_access_server_reports_upgrade_without_mutation_fallback(server):
    # Exercise real legacy route shadowing: /access/{id} sees "surfaces" as an
    # ID on GET and rejects POST with 405, rather than a generic missing URL 404.
    server.app.router.routes = [route for route in server.app.router.routes
                                if not route.path.startswith("/api/v1/access/surfaces")]
    server.app.include_router(access.router, prefix="/api/v1")
    for args in [("add", "sandbox", "must-not-create"), ("ls",), ("providers",)]:
        before = len(server.requests)
        result = server.cli("access", *args, ok=False)
        assert result["error"]["code"] == "SERVER_UPGRADE_REQUIRED"
        assert len(server.requests) == before + 1
    assert not server.surfaces.rows and not server.surfaces.issued
    assert all(path.startswith("/api/v1/access/surfaces") for _, path, _ in server.requests)
