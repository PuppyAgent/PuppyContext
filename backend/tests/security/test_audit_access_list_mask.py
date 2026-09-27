"""ISSUE-002 — access IDOR + credential-free ordinary responses.

Guarantees verified:
  1. A caller passing a project_id they are NOT a member of gets an empty list
     (no IDOR into another tenant's access points).
  2. list/get/update/rename never return raw credentials at any config depth.
  3. metadata updates cannot introduce or rotate a credential.

Hermetic: get_current_user overridden; org/project resolution and the Supabase
client are faked, so no DB is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.access import router as access_router_mod
from src.platform.access.router import router as access_router
from src.exception_handler import app_exception_handler
from src.exceptions import AppException
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.repository_target.protocol import require_repository_target_contract
from tests.authorization_fakes import authorization_for, install_authorization

ALLOWED = "proj-allowed"
FOREIGN = "proj-foreign"


class _FakeConnectorsClient:
    """Small stateful Supabase stub for the four ordinary response paths."""

    def __init__(self, rows):
        self._rows = [dict(row) for row in rows]
        self._table = None
        self._update = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def update(self, values):
        self._update = values
        return self

    def execute(self):
        if self._table == "access_surface_credentials":
            return SimpleNamespace(data=[{
                "id": "credential-1",
                "access_surface_id": "conn-1",
                "credential_type": "bearer_token",
                "credential_lifecycle": "shared",
                "user_id": None,
                "status": "active",
                "key_last4": "WXYZ",
            }])
        if self._update is not None and self._table == "access_surfaces":
            for row in self._rows:
                row.update(self._update)
            self._update = None
        return SimpleNamespace(data=self._rows)


def _install(monkeypatch, rows):
    client = _FakeConnectorsClient(rows)
    monkeypatch.setattr(access_router_mod, "resolve_org_ids", lambda *a, **k: ["org-1"])
    monkeypatch.setattr(access_router_mod, "_get_user_project_ids", lambda *a, **k: [ALLOWED])
    monkeypatch.setattr(
        access_router_mod, "_require_connection_project_access", lambda *a, **k: None
    )
    monkeypatch.setattr(access_router_mod, "_get_client", lambda: client)
    return client


def _app():
    app = FastAPI()
    app.add_exception_handler(AppException, app_exception_handler)
    app.include_router(access_router, prefix="/api/v1")
    app.dependency_overrides[require_repository_target_contract] = lambda: 2
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-alice",
        email="a@example.com",
        role="authenticated",
    )
    install_authorization(app, authorization_for(ALLOWED))
    return app


def _agent_row():
    return {
        "id": "conn-1",
        "project_id": ALLOWED,
        "provider": "agent",
        "name": "My Agent",
        "status": "active",
        "direction": "outbound",
        "scope_id": None,  # keeps _enrich from querying repo_scopes
        "config": {
            "mcp_api_key": "mcpkey_abcd1234WXYZ",
            "name": "My Agent",
            "nested": {
                "oauth": {
                    "clientSecret": "nested-client-secret",
                    "refresh_token": "nested-refresh-token",
                    "region": "sg",
                },
                "items": [
                    {"provider_api_key": "nested-provider-key", "label": "safe"},
                ],
            },
        },
        "trigger": None,
        "last_run_at": None,
        "error_message": None,
        "created_at": None,
        "updated_at": None,
    }


def test_legacy_git_regeneration_is_closed_in_favor_of_idempotent_client_issuance(
    monkeypatch,
):
    row = {
        "id": "surface-git",
        "org_id": "org-1",
        "project_id": ALLOWED,
        "scope_id": "scope-docs",
        "kind": "git_remote",
        "status": "active",
    }

    class _Surfaces:
        def __init__(self, _client):
            pass

        def get(self, connection_id):
            assert connection_id == "surface-git"
            return row

    class _Credentials:
        def __init__(self, _client):
            raise AssertionError("legacy Git credential storage must not be invoked")

    monkeypatch.setattr(access_router_mod, "_get_client", object)
    monkeypatch.setattr(access_router_mod, "AccessSurfaceRepository", _Surfaces)
    monkeypatch.setattr(access_router_mod, "AccessCredentialRepository", _Credentials)
    monkeypatch.setattr(
        access_router_mod, "_require_connection_project_access", lambda *_a, **_k: None
    )
    with TestClient(_app()) as client:
        response = client.post(
            "/api/v1/access/surface-git/regenerate-key",
            json={"grant_mode": "r"},
        )

    assert response.status_code == 410
    assert response.json()["code"] == 1007
    assert "/projects/{project_id}/git-credentials" in response.json()["message"]


def test_foreign_project_returns_empty_no_idor(monkeypatch):
    _install(monkeypatch, rows=[_agent_row()])
    with TestClient(_app()) as tc:
        r = tc.get(f"/api/v1/access/?project_id={FOREIGN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == [], "foreign project_id must yield an empty list (no IDOR)"


def test_member_list_masks_credentials(monkeypatch):
    _install(monkeypatch, rows=[_agent_row()])
    with TestClient(_app()) as tc:
        r = tc.get(f"/api/v1/access/?project_id={ALLOWED}")
    assert r.status_code == 200, r.text
    items = r.json()["data"]
    assert len(items) == 1
    conn = items[0]

    # Raw credential must NOT be present in the list view.
    assert conn["access_key"] is None
    # Masked indicators are present instead.
    assert conn["has_key"] is True
    assert conn["key_last4"] == "WXYZ"
    # And the raw secret must be stripped from the echoed config.
    assert "mcp_api_key" not in (conn.get("config") or {})
    # Non-secret config is preserved.
    assert (conn.get("config") or {}).get("name") == "My Agent"
    assert conn["config"]["nested"]["oauth"] == {"region": "sg"}
    assert conn["config"]["nested"]["items"] == [{"label": "safe"}]


def test_list_response_carries_no_raw_secret_anywhere(monkeypatch):
    """Belt-and-suspenders: the serialized body must not contain the raw key."""
    _install(monkeypatch, rows=[_agent_row()])
    with TestClient(_app()) as tc:
        r = tc.get(f"/api/v1/access/?project_id={ALLOWED}")
    assert "mcpkey_abcd1234WXYZ" not in r.text


def test_list_serializes_each_rows_own_metadata(monkeypatch):
    first = _agent_row()
    first["config"] = {
        **first["config"],
        "direction": "inbound",
        "trigger": {"type": "manual"},
    }
    second = _agent_row()
    second["id"] = "conn-2"
    second["name"] = "Other Agent"
    second["config"] = {
        **second["config"],
        "name": "Other Agent",
        "direction": "outbound",
        "trigger": {"type": "scheduled"},
    }
    _install(monkeypatch, rows=[first, second])

    with TestClient(_app()) as tc:
        response = tc.get(f"/api/v1/access/?project_id={ALLOWED}")

    assert response.status_code == 200, response.text
    by_id = {item["id"]: item for item in response.json()["data"]}
    assert (by_id["conn-1"]["direction"], by_id["conn-1"]["trigger"]) == (
        "inbound",
        {"type": "manual"},
    )
    assert (by_id["conn-2"]["direction"], by_id["conn-2"]["trigger"]) == (
        "outbound",
        {"type": "scheduled"},
    )


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", f"/api/v1/access/?project_id={ALLOWED}", None),
        ("get", "/api/v1/access/conn-1", None),
        ("patch", "/api/v1/access/conn-1", {"status": "inactive"}),
        ("patch", "/api/v1/access/conn-1/rename", {"name": "Renamed"}),
    ],
)
def test_all_ordinary_responses_are_recursively_credential_free(
    monkeypatch,
    method,
    path,
    json_body,
):
    _install(monkeypatch, rows=[_agent_row()])
    with TestClient(_app()) as tc:
        response = tc.request(method, path, json=json_body)

    assert response.status_code == 200, response.text
    for secret in (
        "mcpkey_abcd1234WXYZ",
        "nested-client-secret",
        "nested-refresh-token",
        "nested-provider-key",
    ):
        assert secret not in response.text

    data = response.json()["data"]
    connection = data[0] if isinstance(data, list) else data
    assert connection["access_key"] is None
    assert connection["has_key"] is True
    assert connection["key_last4"] == "WXYZ"


def test_metadata_update_rejects_nested_credentials(monkeypatch):
    _install(monkeypatch, rows=[_agent_row()])
    with TestClient(_app()) as tc:
        response = tc.patch(
            "/api/v1/access/conn-1",
            json={"config": {"safe": {"providerApiKey": "must-not-be-written"}}},
        )

    assert response.status_code == 400, response.text
    assert "dedicated create or regenerate-key flow" in response.text
    assert "must-not-be-written" not in response.text
