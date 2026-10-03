"""Actual Desktop Main/preload/renderer reload against local canonical services.

Opt-in E2E gate: launch Electron with temporary userData and a loopback server.
Binding/run persistence, auth identity and worker/Provider IO are isolated facts;
this is not a production, PostgreSQL, Provider or signed-installer receipt.
"""

import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from src.common_schemas import ApiResponse
from tests.platform.test_synchronize_clients_http import http_server as http_server
from tests.platform.test_synchronize_identity_http import (
    environment as environment,
)
from tests.platform.test_synchronize_public_api import (
    canonical as resource_environment,  # noqa: F401 -- pytest fixture registration
)

pytestmark = pytest.mark.e2e


@pytest.fixture
def canonical(request, monkeypatch):
    app, bindings, runs, queue = request.getfixturevalue("resource_environment")
    # The real source service/PDP/URL provider admission remain mounted. Old
    # resource routes must not be needed by the application window.
    app.router.routes = [
        r for r in app.routes if not getattr(r, "path", "").startswith("/api/v1/integrations")
    ]
    state = {"requests": [], "fail_list": False}
    app.state.window_probe = state
    project = {
        "id": "project-1",
        "name": "Window acceptance",
        "description": None,
        "org_id": "org-1",
        "effective_role": "admin",
        "capabilities": [
            "project.read",
            "project.manage",
            "synchronize.manage",
            "access_surface.manage",
            "history.read",
        ],
    }
    target = {"kind": "project_root", "project_id": "project-1"}

    @app.middleware("http")
    async def record(request, call_next):
        if request.url.path.startswith("/api/v1"):
            state["requests"].append(
                {"method": request.method, "path": request.url.path, "query": request.url.query}
            )
            if request.url.path not in {"/api/v1/auth/refresh"}:
                assert request.headers.get("authorization") == "Bearer isolated-window-access"
        if (
            state["fail_list"]
            and request.method == "GET"
            and request.url.path == "/api/v1/synchronize/bindings"
        ):
            return JSONResponse(
                status_code=503, content={"detail": "Window fixture inventory unavailable"}
            )
        return await call_next(request)

    @app.post("/api/v1/auth/refresh")
    async def refresh(request: Request):
        assert (await request.json())["refresh_token"] == "isolated-window-refresh"
        return ApiResponse.success(
            {
                "access_token": "isolated-window-access",
                "refresh_token": "isolated-window-refresh",
                "user_id": "user-1",
                "user_email": "window@example.test",
                "expires_in": 3600,
            }
        )

    @app.post("/api/v1/auth/initialize")
    def initialize():
        return ApiResponse.success({})

    @app.post("/api/v1/projects/project-1/repository-context")
    async def context(request: Request):
        assert (await request.json())["target"] == target
        return ApiResponse.success({"project": project, "target": target, "scope": None})

    @app.get("/api/v1/projects/project-1")
    def get_project():
        return ApiResponse.success(project)

    @app.get("/api/v1/projects/")
    def projects():
        return ApiResponse.success([project])

    @app.get("/api/v1/projects/project-1/dashboard/resources")
    def dashboard():
        return ApiResponse.success(
            {
                "project": project,
                "nodes": {"total": 0, "folders": 0, "files": 0},
                "resources": [
                    {
                        "resource_kind": "synchronize",
                        "resource_id": b.id,
                        "project_id": b.project_id,
                        "provider": b.provider,
                        "path": b.path,
                        "status": b.status,
                        "name": b.provider,
                        "usage_buckets": [0] * 14,
                    }
                    for b in bindings.items.values()
                ],
                "tools": [],
                "uploads": [],
            }
        )

    @app.get("/api/v1/content/project-1/ls")
    def content():
        return ApiResponse.success({"project_id": "project-1", "path": "", "entries": []})

    @app.get("/api/v1/projects/project-1/scopes")
    @app.get("/api/v1/projects/project-1/access/surfaces")
    @app.get("/api/v1/mcp-endpoints")
    @app.get("/api/v1/organizations/")
    def empty_metadata():
        return ApiResponse.success([])

    @app.get("/api/v1/projects/project-1/access-point")
    def identity(request: Request):
        return ApiResponse.success(
            {
                "project_id": "project-1",
                "target": target,
                "git_url": str(request.base_url).rstrip("/") + "/git/project-1.git",
            }
        )

    @app.get("/__window/state")
    def snapshot():
        return {
            "bindings": [asdict(b) for b in bindings.items.values()],
            "runs": [
                r.model_dump() if hasattr(r, "model_dump") else asdict(r)
                for r in runs.items.values()
            ],
            **state,
        }

    @app.post("/__window/fail-run")
    def fail_run():
        assert len(bindings.items) == 1 and len(runs.items) == 1
        binding = next(iter(bindings.items.values()))
        binding.status = "error"
        binding.error_message = "Isolated Provider failure for window retry"
        run = next(iter(runs.items.values()))
        run.status = "failed"
        run.error = binding.error_message
        run.result_summary = "Window first run failed"
        return {"binding_id": binding.id, "run_id": run.id}

    @app.post("/__window/inventory/{available}")
    def availability(available: str):
        state["fail_list"] = available != "up"
        return {"ok": True}

    def forbidden(*args, **kwargs):
        raise AssertionError("External Python HTTP is forbidden during window acceptance")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    return app, bindings, runs, queue


def test_creation_real_window_reload_and_failed_retry_keep_binding_and_history(
    http_server, canonical, tmp_path
):
    source = os.environ.get("PUPPYONE_DESKTOP_SOURCE")
    assert source, (
        "Set PUPPYONE_DESKTOP_SOURCE to the Desktop worktree for this explicit window gate"
    )
    root = Path(source).resolve()
    driver = root / "tests/e2e/cloud/entrypoint-window.smoke.mjs"
    assert driver.is_file()
    try:
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import electron from 'electron'; import {spawn} from 'node:child_process'; "
                "const child=spawn(electron,[process.argv[1]],{stdio:'inherit',env:process.env}); "
                "child.on('exit',(code)=>process.exit(code??1));",
                str(driver),
            ],
            cwd=root,
            env={
                **os.environ,
                "PUPPYONE_ENTRYPOINT_TEST_ORIGIN": http_server,
                "PUPPYONE_ENTRYPOINT_TEST_REPORT": str(tmp_path / "window-report.json"),
                "PUPPYONE_ENTRYPOINT_TEST_WORKSPACE": str(tmp_path / "desktop-runtime"),
            },
            capture_output=True,
            text=True,
            timeout=240,
        )
    finally:
        # Production Main may flush files at process exit, after the driver's
        # first cleanup. The parent owns this disposable directory only.
        shutil.rmtree(tmp_path / "desktop-runtime", ignore_errors=True)
    assert not (tmp_path / "desktop-runtime").exists()
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "window-report.json").read_text())
    assert report["ok"] and report["window_reloads"] == 3
    assert len(set(report["document_ids"])) == report["window_reloads"]
    _, bindings, runs, queue = canonical
    assert len(bindings.items) == 1 and set(runs.items) == {"run-1", "run-2"}
    binding_id = next(iter(bindings.items))
    assert report["binding_id"] == binding_id
    assert all(r.connection_id == binding_id for r in runs.items.values())
    assert queue.enqueue_sync_run.call_count == 2
    assert not any(
        "/integrations" in r["path"] or "/connectors" in r["path"]
        for r in canonical[0].state.window_probe["requests"]
    )
