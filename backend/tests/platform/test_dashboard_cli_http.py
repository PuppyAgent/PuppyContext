"""Actual CLI -> canonical Dashboard HTTP/policy/repositories; storage is isolated.

No old Dashboard route is mounted for success cases. This is not live DB/JWT or
installed-client evidence, and cannot certify ISSUE-061's worker cutover.
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.platform.project import resource_dashboard as dashboard
from tests.platform.test_resource_dashboard import (
    environment as dashboard_environment,  # noqa: F401 -- fixture registration
)
from tests.platform.test_resource_dashboard import facts
from tests.platform.test_synchronize_clients_http import http_server as http_server
from tests.version_engine.test_dashboard_usage_buckets import FakeSB

pytestmark = pytest.mark.integration
CLI = Path(__file__).resolve().parents[3] / "cli/bin/puppyone.js"


@pytest.fixture
def canonical(request):
    (app,) = request.getfixturevalue("dashboard_environment")
    requests = []

    @app.middleware("http")
    async def record(request, call_next):
        requests.append((request.method, request.url.path))
        return await call_next(request)

    return app, requests


@pytest.fixture
def cli(http_server, tmp_path):
    assert (CLI.parents[1] / "node_modules/commander").exists(), "Install CLI dependencies"

    def run(*, project="project-1", json_mode=True, ok=True):
        result = subprocess.run(
            ["node", str(CLI), *(["--json"] if json_mode else []), "--api-url", http_server,
             "--api-key", "isolated-test-token", "--project", project, "status"],
            text=True, capture_output=True, timeout=20,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert result.returncode == (0 if ok else 1), result.stdout + result.stderr
        if json_mode:
            value = json.loads(result.stdout if ok else result.stderr)
            assert value["success"] is ok
            return value
        return result.stdout + result.stderr

    return run


def test_status_preserves_colliding_resource_ids_and_project_policy(cli, canonical):
    data = cli()["dashboard"]
    assert [(row["resource_kind"], row["resource_id"]) for row in data["resources"]] == [
        ("synchronize", "same"), ("access", "same"), ("access", "mcp"),
    ]
    assert data["resources"][0]["usage_buckets"][-1] == 1
    assert data["resources"][1]["usage_buckets"][-1] == 2
    assert data["resources"][2]["target"]["scope_id"] == "scope-1"
    assert "connections" not in data and "access_points" not in data
    assert "must-not-leak" not in json.dumps(data)
    text = cli(json_mode=False)
    assert "synchronize:same" in text and "access:same" in text and "scope:scope-1" in text
    denied = cli(project="foreign", ok=False)
    assert denied["error"]["code"] != "SERVER_UPGRADE_REQUIRED"
    assert len(canonical[1]) == 3
    assert all(method == "GET" and path.endswith("/dashboard/resources") for method, path in canonical[1])


def test_status_classification_and_storage_failures_are_not_empty_success(cli, canonical, monkeypatch):
    data = facts()
    data["connections"][0]["config"] = {"db_config": {"api_key": "private-source-secret"}}
    monkeypatch.setattr(dashboard, "SupabaseClient", lambda: SimpleNamespace(client=FakeSB(data)))
    rejected = cli(ok=False)
    assert "SOURCE_CLASSIFICATION_REQUIRED" in json.dumps(rejected)
    assert "private-source-secret" not in json.dumps(rejected)

    def unavailable(*args):
        raise RuntimeError("isolated storage unavailable")

    monkeypatch.setattr(dashboard, "fetch_dashboard_resources", unavailable)
    failed = cli(ok=False)
    assert "dashboard" not in failed
    assert len(canonical[1]) == 2


def test_status_old_server_requires_upgrade_without_legacy_fallback(cli, canonical):
    app, requests = canonical
    app.router.routes = [route for route in app.router.routes if not route.path.endswith("/dashboard/resources")]

    @app.get("/api/v1/projects/{project_id}/dashboard")
    def old_dashboard(project_id: str):
        pytest.fail("CLI must never fall back to ambiguous old Dashboard")

    result = cli(ok=False)
    assert result["error"]["code"] == "SERVER_UPGRADE_REQUIRED"
    assert requests == [("GET", "/api/v1/projects/project-1/dashboard/resources")]
