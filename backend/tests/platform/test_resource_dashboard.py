"""Dashboard inventory/usage is keyed by resource domain, never by bare ID."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.project import resource_dashboard as dashboard
from src.platform.project.dependencies import get_project_repository
from src.version_engine.bootstrap.dependencies import get_product_operation_adapter
from tests.authorization_fakes import authorization_for, install_authorization
from tests.version_engine.test_dashboard_usage_buckets import FakeSB, FakeTable


def facts():
    now = datetime.now(UTC).isoformat()
    return {
        "connections": [
            {
                "id": "same",
                "project_id": "project-1",
                "provider": "url",
                "target_path": "destination",
                "scope_id": "unrelated-scope",
                "status": "active",
                "config": {},
                "created_at": now,
            }
        ],
        "access_surfaces": [
            {
                "id": "same",
                "project_id": "project-1",
                "kind": "agent",
                "scope_id": None,
                "name": "Same name",
                "status": "paused",
                "config": {"api_key": "must-not-leak"},
                "created_at": now,
            },
            {
                "id": "mcp",
                "project_id": "project-1",
                "kind": "mcp",
                "scope_id": "scope-1",
                "name": "MCP",
                "status": "active",
                "config": {},
                "created_at": now,
            },
        ],
        "repository_scopes": [
            {"id": "scope-1", "project_id": "project-1", "path": "destination", "max_mode": "r"}
        ],
        "sync_runs": [
            {"connection_id": "same", "project_id": "project-1", "started_at": now},
            {"connection_id": "same", "project_id": "foreign", "started_at": now},
        ],
        "agent_execution_logs": [
            {"agent_id": "same", "started_at": now},
            {"agent_id": "same", "started_at": now},
        ],
    }


def test_cross_domain_id_collision_never_joins_usage_status_or_target():
    result = dashboard.fetch_dashboard_resources(FakeSB(facts()), "project-1")
    assert [(row.resource_kind, row.resource_id) for row in result] == [
        ("synchronize", "same"),
        ("access", "same"),
        ("access", "mcp"),
    ]
    assert result[0].usage_buckets[-1] == 1 and result[1].usage_buckets[-1] == 2
    assert sum(result[2].usage_buckets) == 0
    assert result[0].path == "destination" and result[0].status == "active"
    assert (
        result[1].target.kind == "project_root"
        and result[1].path == ""
        and result[1].status == "paused"
    )
    assert result[2].target.scope_id == "scope-1" and result[2].scope_mode == "r"
    assert all("must-not-leak" not in row.model_dump_json() for row in result)
    assert all(
        "access_key" not in row.model_dump() and "id" not in row.model_dump() for row in result
    )


def test_scope_only_cannot_fabricate_a_dashboard_resource():
    assert (
        dashboard.fetch_dashboard_resources(
            FakeSB({"repository_scopes": facts()["repository_scopes"]}), "project-1"
        )
        == []
    )


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {"id": "scope-1", "project_id": "foreign", "path": "destination"},
        {"id": "scope-1", "project_id": "project-1", "path": ""},
    ],
)
def test_missing_foreign_or_synthetic_root_scope_is_not_reinterpreted(scope):
    data = facts()
    data["repository_scopes"] = [scope] if scope else []
    with pytest.raises(HTTPException) as error:
        dashboard.fetch_dashboard_resources(FakeSB(data), "project-1")
    assert error.value.status_code == 409


def test_ambiguous_mixed_storage_fails_closed_without_disclosing_source_config():
    data = facts()
    data["connections"][0]["config"] = {"db_config": {"api_key": "private-import-key"}}
    with pytest.raises(HTTPException) as error:
        dashboard.fetch_dashboard_resources(FakeSB(data), "project-1")
    assert error.value.status_code == 409 and "private-import-key" not in str(error.value)


def test_failed_usage_read_is_not_reported_as_zero_successful_activity():
    class BrokenTable(FakeTable):
        def execute(self):
            raise RuntimeError("unavailable")

    class BrokenSB(FakeSB):
        def table(self, name):
            return BrokenTable([]) if name == "agent_execution_logs" else super().table(name)

    with pytest.raises(RuntimeError, match="unavailable"):
        dashboard.fetch_dashboard_resources(BrokenSB(facts()), "project-1")


@pytest.fixture
def environment(monkeypatch):
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/v1")
    install_authorization(app, authorization_for("project-1", role="viewer"))
    storage = FakeSB(facts())
    monkeypatch.setattr(dashboard, "SupabaseClient", lambda: SimpleNamespace(client=storage))
    monkeypatch.setattr(
        dashboard, "_compute_node_counts", lambda *_: dashboard.DashboardNodeCounts()
    )
    monkeypatch.setattr(dashboard, "_fetch_tools", lambda *_: [])
    monkeypatch.setattr(dashboard, "_fetch_uploads", lambda *_: [])
    app.dependency_overrides[get_product_operation_adapter] = lambda: object()
    app.dependency_overrides[get_project_repository] = lambda: SimpleNamespace(
        get_by_id=lambda project_id: SimpleNamespace(
            id=project_id, name="Project", description=None
        )
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-1", role="authenticated"
    )
    return (app,)


def test_canonical_dashboard_http_policy_selectors_and_response(environment):
    (app,) = environment
    with TestClient(app) as client:
        result = client.get("/api/v1/projects/project-1/dashboard/resources")
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        assert len(data["resources"]) == 3 and "connections" not in data
        assert (
            client.get(
                "/api/v1/projects/project-1/dashboard/resources?connection_id=same"
            ).status_code
            == 422
        )
        assert client.get("/api/v1/projects/foreign/dashboard/resources").status_code in (403, 404)
