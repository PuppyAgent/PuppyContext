"""Real Access routers, service and Project policy with isolated storage/issuance."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.access import project_router as legacy_project
from src.platform.access import public_router as public
from src.platform.access import router as legacy_global
from src.platform.access.models import AccessSurface
from src.platform.access.service import AccessService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.entitlements.dependencies import get_entitlement_service
from src.platform.project import repository as project_repository
from src.platform.project.dependencies import get_project_repository
from src.platform.repository_target.models import ProjectRootTarget, ScopeTarget
from tests.authorization_fakes import authorization_for, install_authorization

BASE = "/api/v1/access/surfaces"
PROJECT = "/api/v1/projects/project-1/access/surfaces"
HEADERS = {"X-PuppyOne-Repository-Contract": "2"}
NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Memory:
    def __init__(self):
        self.items = {}
        self.scopes = {
            "scope-1": SimpleNamespace(
                id="scope-1", project_id="project-1", path="docs", name="Docs"
            ),
            "scope-2": SimpleNamespace(
                id="scope-2", project_id="project-2", path="private", name="Private"
            ),
        }
        self.issuances = []
        self.sequence = 0

    def get(self, surface_id):
        return self.items.get(surface_id)

    def insert(
        self,
        project_id,
        scope_id=None,
        kind="mcp",
        name="MCP",
        direction="outbound",
        config=None,
        policy=None,
        oauth_connection_id=None,
        trigger=None,
        created_by="user-1",
    ):
        self.sequence += 1
        surface = AccessSurface(
            id=f"surface-{self.sequence}",
            target=ScopeTarget(project_id=project_id, scope_id=scope_id)
            if scope_id
            else ProjectRootTarget(project_id=project_id),
            kind=kind,
            name=name,
            direction=direction,
            config=config or {},
            policy=policy or {},
            oauth_connection_id=oauth_connection_id,
            trigger=trigger or {"type": "manual"},
            status="active",
            last_run_at=None,
            last_run_id=None,
            error_message=None,
            created_by=created_by,
            created_at=NOW,
            updated_at=NOW,
        )
        self.items[surface.id] = surface
        return surface

    def list_by_project(self, project_id, scope_id=None, kind=None, direction=None):
        return [
            item
            for item in self.items.values()
            if item.project_id == project_id
            and (scope_id is None or item.scope_id == scope_id)
            and (kind is None or item.kind == kind)
            and (direction is None or item.direction == direction)
        ]

    def update(self, surface_id, patch):
        current = self.items[surface_id]
        values = dict(patch)
        if "config" in values:
            values["config"] = {**current.config, **(values["config"] or {})}
        self.items[surface_id] = replace(current, **values)
        return self.items[surface_id]

    def delete(self, surface_id):
        return self.items.pop(surface_id, None) is not None

    def raw(self, surface):
        if surface is None:
            return None
        return {
            "id": surface.id,
            "project_id": surface.project_id,
            "org_id": "org-1",
            "scope_id": surface.scope_id,
            "kind": surface.kind,
            "name": surface.name,
            "status": surface.status,
            "config": {
                **surface.config,
                "direction": surface.direction,
                "policy": surface.policy,
                "trigger": surface.trigger,
                "last_run_at": surface.last_run_at,
            },
            "created_at": surface.created_at,
            "updated_at": surface.updated_at,
        }


@pytest.fixture
def environment(monkeypatch):
    memory = Memory()

    class RawSurfaces:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, surface_id):
            return memory.raw(memory.get(surface_id))

        def list_by_projects(self, project_ids, kind=None, status=None):
            return [
                memory.raw(item)
                for item in memory.items.values()
                if item.project_id in project_ids
                and (kind is None or item.kind == kind)
                and (status is None or item.status == status)
            ]

        def list_by_project(self, project_id, kind=None):
            return self.list_by_projects([project_id], kind)

        def scope_rows_for(self, rows):
            return {
                row["scope_id"]: vars(memory.scopes[row["scope_id"]])
                for row in rows
                if row.get("scope_id") in memory.scopes
            }

        def count_user_surfaces_by_project(self, project_id):
            return len(memory.list_by_project(project_id))

        def update(self, surface_id, patch):
            return memory.raw(memory.update(surface_id, patch))

        def delete(self, surface_id):
            return memory.delete(surface_id)

        def ensure_target_defaults(self, project_id, scope, created_by):
            scope_id = scope.id if scope else None
            for kind in ("git_remote", "cli"):
                if self.get_by_target_kind(project_id, scope_id, kind) is None:
                    memory.insert(
                        project_id,
                        scope_id,
                        kind=kind,
                        name=kind,
                        direction="bidirectional",
                        created_by=created_by,
                    )

        def get_by_target_kind(self, project_id, scope_id, kind):
            return next(
                (
                    memory.raw(item)
                    for item in memory.items.values()
                    if (item.project_id, item.scope_id, item.kind) == (project_id, scope_id, kind)
                ),
                None,
            )

    class Credentials:
        def __init__(self, *args, **kwargs):
            pass

        def list_active_by_surface(self, ids):
            return {
                surface_id: {"key_last4": "TEST"}
                for surface_id in ids
                if surface_id in memory.issuances
            }

        def issue_bearer_token(self, **kwargs):
            memory.issuances.append(kwargs["access_surface_id"])
            return "issued-test-credential"

    project_repo = SimpleNamespace(
        get_by_id=lambda project_id: SimpleNamespace(id=project_id, org_id="org-1")
    )
    service = AccessService(
        repository=memory,
        surface_repository=RawSurfaces(),
        scope_repository=SimpleNamespace(get=memory.scopes.get),
    )
    monkeypatch.setattr(legacy_global, "AccessSurfaceRepository", RawSurfaces)
    monkeypatch.setattr(legacy_global, "AccessCredentialRepository", Credentials)
    monkeypatch.setattr("src.repo.access_credentials.AccessCredentialRepository", Credentials)
    monkeypatch.setattr(legacy_global, "_get_client", object)
    monkeypatch.setattr(legacy_global, "resolve_org_ids", lambda *a, **k: ["org-1"])
    # Only inventory discovery is faked; actual ProjectGrant policy still filters it.
    monkeypatch.setattr(
        legacy_global,
        "_get_user_project_ids",
        lambda sb, org, auth, user: auth.accessible_project_ids(["project-1", "project-2"], user),
    )
    monkeypatch.setattr(project_repository, "ProjectRepositorySupabase", lambda: project_repo)

    def configure(payload, **kwargs):
        row = memory.insert(
            payload.project_id, kind=payload.provider, name=payload.name or payload.provider
        )
        return legacy_global.UnifiedConnectionOut(
            id=row.id,
            project_id=row.project_id,
            provider=row.kind,
            name=row.name,
            mcp_api_key="one-time-test-token" if row.kind in {"agent", "mcp"} else None,
            mcp_server_url="https://example.test/mcp" if row.kind in {"agent", "mcp"} else None,
        )

    for name in ("_create_agent", "_create_mcp", "_create_sandbox"):
        monkeypatch.setattr(legacy_global, name, configure)
    entitlements = Mock()
    app = FastAPI()
    app.include_router(public.router, prefix="/api/v1")
    app.include_router(public.project_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-1", email="user@example.test", role="authenticated"
    )
    app.dependency_overrides[get_project_repository] = lambda: project_repo
    app.dependency_overrides[legacy_project.get_access_service] = lambda: service
    app.dependency_overrides[get_entitlement_service] = lambda: entitlements
    install_authorization(app, authorization_for("project-1"))
    return app, memory, service, entitlements


def test_project_create_reload_manage_and_global_read_are_the_same_surface(environment):
    app, memory, _, _ = environment
    with TestClient(app, headers=HEADERS) as client:
        created = client.post(
            PROJECT,
            json={
                "kind": "mcp",
                "direction": "outbound",
                "target": {"kind": "scope", "project_id": "project-1", "scope_id": "scope-1"},
                "config": {"metadata": {"connection_id": "user-value"}},
            },
        )
        assert created.status_code == 201, created.text
        surface = created.json()["data"]
        surface_id = surface["id"]
        assert (
            surface["kind"] == "mcp" and "provider" not in surface and "last_run_id" not in surface
        )
        assert surface["project_id"] == surface["target"]["project_id"] == "project-1"
    with TestClient(app, headers=HEADERS) as client:
        assert client.get(PROJECT).json()["data"][0]["id"] == surface_id
        detail = client.get(f"{BASE}/{surface_id}").json()["data"]
        assert detail["target"] == surface["target"]
        assert detail["config"]["metadata"] == {"connection_id": "user-value"}
        assert client.patch(f"{PROJECT}/{surface_id}", json={"name": "Renamed"}).status_code == 200
        assert client.post(f"{PROJECT}/{surface_id}/pause").status_code == 200
        assert memory.get(surface_id).status == "paused"
        assert client.post(f"{PROJECT}/{surface_id}/resume").status_code == 200
        assert memory.get(surface_id).status == "active"
        assert client.delete(f"{PROJECT}/{surface_id}").status_code == 200
        assert client.get(PROJECT).json()["data"] == []


def test_global_static_catalog_create_and_one_time_credentials(environment):
    app, memory, _, entitlements = environment
    with TestClient(app, headers=HEADERS) as client:
        assert [row["kind"] for row in client.get(BASE + "/types").json()["data"]] == [
            "agent",
            "mcp",
            "sandbox",
        ]
        created = client.post(
            BASE, json={"project_id": "project-1", "kind": "mcp", "name": "Endpoint"}
        )
        assert created.status_code == 201, created.text
        result = created.json()["data"]
        assert result["mcp_api_key"] == "one-time-test-token" and result["kind"] == "mcp"
        assert "provider" not in result and "git_credential" not in result
        for path in (BASE, f"{BASE}/{result['id']}", PROJECT):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert "one-time-test-token" not in response.text and "mcp_api_key" not in response.text
        assert (
            client.patch(f"{BASE}/{result['id']}/rename", json={"name": "New name"}).status_code
            == 200
        )
        assert client.patch(f"{BASE}/{result['id']}", json={"status": "paused"}).status_code == 200
        assert memory.get(result["id"]).status == "paused"
        assert client.post(BASE, json={"project_id": "project-1", "kind": "mcp"}).status_code == 409
        assert client.delete(f"{BASE}/{result['id']}").status_code == 200
    entitlements.require_capacity.assert_called_once()


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_management_and_credential_issuance_require_human_project_authority(environment, role):
    app, memory, _, entitlements = environment
    row = memory.insert("project-1", kind="cli", direction="bidirectional")
    install_authorization(app, authorization_for("project-1", role=role))
    with TestClient(app, headers=HEADERS) as client:
        assert client.get(PROJECT).status_code == 200
        assert (
            client.post(BASE, json={"project_id": "project-1", "kind": "agent"}).status_code == 403
        )
        assert client.post(f"{BASE}/{row.id}/regenerate-key").status_code == 403
        assert client.patch(f"{PROJECT}/{row.id}", json={"status": "paused"}).status_code == 403
        assert client.delete(f"{BASE}/{row.id}").status_code == 403
    assert set(memory.items) == {row.id}
    assert memory.get(row.id).status == "active" and memory.issuances == []
    entitlements.require_capacity.assert_not_called()


def test_rotation_is_explicit_bound_to_access_and_git_stays_client_generated(environment):
    app, memory, _, _ = environment
    cli = memory.insert("project-1", kind="cli", direction="bidirectional")
    git = memory.insert("project-1", kind="git_remote", direction="bidirectional")
    with TestClient(app, headers=HEADERS) as client:
        response = client.post(f"{BASE}/{cli.id}/regenerate-key")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["access_surface_id"] == cli.id
        assert response.json()["data"]["credential"] == "issued-test-credential"
        assert client.post(f"{BASE}/{git.id}/regenerate-key").status_code == 410
        assert "issued-test-credential" not in client.get(f"{BASE}/{cli.id}").text
        assert client.delete(f"{BASE}/{git.id}").status_code == 400
        assert client.delete(f"{PROJECT}/{git.id}").status_code == 400
    assert memory.issuances == [cli.id]


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "scope", "project_id": "project-1", "scope_id": "missing"},
        {"kind": "scope", "project_id": "project-1", "scope_id": "scope-2"},
        {"kind": "project_root", "project_id": "project-2"},
        {"kind": "project_root", "project_id": "project-1", "scope_id": "scope-2"},
        {"kind": "scope", "project_id": "project-1", "scope_id": ""},
        {"kind": "scope", "project_id": "project-1"},
    ],
)
def test_invalid_target_is_never_reinterpreted_as_project_root(environment, target):
    app, memory, _, _ = environment
    with TestClient(app, headers=HEADERS) as client:
        result = client.post(
            PROJECT, json={"kind": "mcp", "direction": "outbound", "target": target}
        )
        assert result.status_code in (404, 422), result.text
        assert client.post(PROJECT + "/enable-target", json={"target": target}).status_code in (
            404,
            422,
        )
    assert memory.items == {}


def test_enable_target_is_idempotent_and_does_not_return_credentials(environment):
    app, memory, _, _ = environment
    with TestClient(app, headers=HEADERS) as client:
        body = {"target": {"kind": "project_root", "project_id": "project-1"}}
        first = client.post(PROJECT + "/enable-target", json=body)
        second = client.post(PROJECT + "/enable-target", json=body)
        assert first.status_code == second.status_code == 200
        assert first.json()["data"] == second.json()["data"]
        assert {row["kind"] for row in first.json()["data"]} == {"cli", "git_remote"}
        assert len(memory.items) == 2 and memory.issuances == []


@pytest.mark.parametrize(
    "query",
    [
        "provider=cli",
        "connection_id=wrong",
        "kind=",
        "kind=cli&kind=agent",
        "include_non_access=true",
    ],
)
def test_invalid_selectors_do_not_expand_inventory(environment, query):
    app, _, _, _ = environment
    with TestClient(app, headers=HEADERS) as client:
        assert client.get(PROJECT + "?" + query).status_code == 422
        assert client.get(BASE + "?" + query).status_code == 422


def test_wrong_domain_foreign_and_legacy_surface_ids_cannot_manage_resources(environment):
    app, memory, _, _ = environment
    foreign = memory.insert("project-2")
    legacy = memory.insert("project-1", kind="gmail")
    with TestClient(app, headers=HEADERS) as client:
        denied = client.get(BASE + "?project_id=project-2")
        assert denied.status_code in (403, 404)
        assert denied.json().get("data") is None
        assert client.get(PROJECT).json()["data"] == []
        assert client.get(BASE).json()["data"] == []
        for surface_id, expected in (
            ("synchronize-only", 404),
            (foreign.id, 404),
            (legacy.id, 409),
        ):
            assert client.post(f"{PROJECT}/{surface_id}/pause").status_code == expected
            assert client.delete(f"{PROJECT}/{surface_id}").status_code == expected
        assert client.delete(f"{BASE}/{legacy.id}").status_code == 409
        assert client.get(f"{BASE}/{foreign.id}").status_code in (403, 404)
        assert client.post(f"{PROJECT}/{legacy.id}/run").status_code == 404
    assert set(memory.items) == {foreign.id, legacy.id}


@pytest.mark.parametrize("field", ["config", "policy", "trigger"])
def test_canonical_metadata_stays_private_and_retired_routes_cannot_write(
    environment, field
):
    app, memory, _, _ = environment
    row = memory.insert(
        "project-1",
        config={
            "nested": {"clientSecret": "forbidden-config-secret", "connection_id": "user-field"}
        },
        policy={"nested": {"api_key": "forbidden-policy-secret"}},
        trigger={"type": "manual", "config": {"bearer_token": "forbidden-trigger-secret"}},
    )
    with TestClient(app, headers=HEADERS) as client:
        for path in (PROJECT, BASE, f"{BASE}/{row.id}"):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert "forbidden-" not in response.text
        payload = {field: {"nested": {"api_key": "new-test-secret"}}}
        if field == "trigger":
            payload = {field: {"type": "manual", "config": {"api_key": "new-test-secret"}}}
        bases = [PROJECT]
        if field != "policy":
            bases.append(BASE)
        for base in bases:
            assert client.patch(f"{base}/{row.id}", json=payload).status_code == 400
        for base in ("/api/v1/projects/project-1/connectors", "/api/v1/access"):
            assert client.get(f"{base}/{row.id}").status_code == 404
            assert client.patch(f"{base}/{row.id}", json=payload).status_code == 404
        assert (
            client.post(
                BASE,
                json={
                    "project_id": "project-1",
                    "kind": "mcp",
                    "config": {"api_key": "new-test-secret"},
                },
            ).status_code
            == 400
        )
    assert "new-test-secret" not in str(memory.items)
    assert set(memory.items) == {row.id}


def test_no_legacy_body_alias_or_implicit_target_change(environment):
    app, memory, _, _ = environment
    row = memory.insert("project-1")
    with TestClient(app, headers=HEADERS) as client:
        assert (
            client.post(BASE, json={"project_id": "project-1", "provider": "mcp"}).status_code
            == 422
        )
        assert (
            client.post(BASE, json={"project_id": "project-1", "kind": "gmail"}).status_code == 422
        )
        for field, value in (
            ("kind", "agent"),
            ("provider", "agent"),
            ("target", {"kind": "project_root", "project_id": "project-2"}),
        ):
            assert client.patch(f"{PROJECT}/{row.id}", json={field: value}).status_code == 422
        assert (
            client.post(
                BASE, json={"project_id": "project-1", "kind": "mcp", "sync_mode": "manual"}
            ).status_code
            == 422
        )
    assert memory.get(row.id).project_id == "project-1" and memory.get(row.id).kind == "mcp"


@pytest.mark.parametrize("base", [BASE, PROJECT])
def test_metadata_cannot_turn_access_into_an_import_record(environment, base):
    app, memory, _, _ = environment
    row = memory.insert("project-1")
    before = asdict(row)
    with TestClient(app, headers=HEADERS) as client:
        response = client.patch(f"{base}/{row.id}", json={"trigger": {"type": "import_once"}})
        assert response.status_code == 422, response.text
    assert asdict(memory.get(row.id)) == before


def test_contract_v2_header_is_still_required(environment):
    app, _, _, _ = environment
    with TestClient(app) as client:
        assert client.get(PROJECT).status_code == 426
        assert client.get(BASE).status_code == 426
