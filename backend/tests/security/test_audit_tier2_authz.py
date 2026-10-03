"""Tier-2 audit — authorization/redaction fixes for P0/P1 bugs.

Covers (see fix/audit-security-tier2):
  1. integrations trigger_push (POST /push/{path}) now enforces project access.
  2. project update/delete/seed now require owner/admin (plain members → 403).
  3. connectors/manager get_connection detail now masks credentials.
  5. internal table context endpoints now enforce acting-user project access.
  6. integrations get_connection_run now scopes the run to an accessible project.

Hermetic: get_current_user overridden; project/org resolution + Supabase client
faked, so no DB is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.exceptions import AppException
from src.exception_handler import app_exception_handler
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.repository_target.protocol import require_repository_target_contract
from tests.authorization_fakes import authorization_for, install_authorization

ALLOWED = "proj-allowed"
FOREIGN = "proj-foreign"


def _user():
    return CurrentUser(user_id="user-alice", email="a@example.com", role="authenticated")


def _base_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppException, app_exception_handler)
    app.dependency_overrides[require_repository_target_contract] = lambda: 2
    app.dependency_overrides[get_current_user] = _user
    install_authorization(app, authorization_for(ALLOWED))
    return app


# ── Bug 1: trigger_push project access ───────────────────────────────

def _integrations_app(verify_returns):
    from src.platform.synchronize.public_router import router as integ_router

    app = _base_app()
    app.include_router(integ_router, prefix="/api/v1")
    return app, MagicMock()


def test_trigger_push_foreign_project_forbidden():
    app, _svc = _integrations_app(lambda pid, uid: None)  # never a member
    with TestClient(app) as tc:
        r = tc.post(f"/api/v1/synchronize/push?path=notes/a.md&project_id={FOREIGN}")
    assert r.status_code == 404, r.text


# ── Bug 2: project update/delete/seed owner-admin gate ───────────────

def _project_app(role):
    from src.platform.project.router import router as project_router
    from src.platform.project.dependencies import (
        get_project_repository,
        get_project_service,
    )

    app = _base_app()
    app.include_router(project_router, prefix="/api/v1")
    install_authorization(app, authorization_for(ALLOWED, role=role))

    fake_project = SimpleNamespace(
        id=ALLOWED, name="P", description="", org_id="org-1",
        visibility="private", bound_git_branch="main", updated_at=None,
    )

    svc = MagicMock()
    svc.update.return_value = fake_project
    svc.delete.return_value = None

    repository = MagicMock()
    repository.get_by_id.return_value = fake_project
    app.dependency_overrides[get_project_repository] = lambda: repository
    app.dependency_overrides[get_project_service] = lambda: svc
    return app, svc


@pytest.mark.parametrize("method,path,body", [
    ("put", f"/api/v1/projects/{ALLOWED}", {"name": "x"}),
    ("delete", f"/api/v1/projects/{ALLOWED}", None),
])
def test_project_mutations_reject_plain_member(method, path, body):
    app, svc = _project_app(role="editor")  # member but not owner/admin
    with TestClient(app) as tc:
        r = getattr(tc, method)(path, json=body) if body is not None else getattr(tc, method)(path)
    assert r.status_code == 403, r.text
    svc.delete.assert_not_called()


def test_project_update_allows_admin():
    app, svc = _project_app(role="admin")
    with TestClient(app) as tc:
        r = tc.put(f"/api/v1/projects/{ALLOWED}", json={"name": "x"})
    assert r.status_code == 200, r.text
    svc.update.assert_called_once()


# ── Bug 3: get_connection detail masks credentials ───────────────────

class _FakeConnClient:
    def __init__(self, row):
        self._row = row
        self._table = None

    def table(self, name, *a, **k):
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

    def execute(self):
        if self._table == "access_surface_credentials":
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[self._row])


def _conn_row():
    return {
        "id": "conn-1",
        "project_id": ALLOWED,
        "kind": "agent",
        "name": "My Agent",
        "status": "active",
        "direction": "outbound",
        "scope_id": None,
        "config": {"mcp_api_key": "mcpkey_abcd1234WXYZ", "name": "My Agent"},
        "trigger": None,
        "last_run_at": None,
        "error_message": None,
        "created_at": None,
        "updated_at": None,
    }


def test_get_connection_detail_masks_credentials(monkeypatch):
    from src.platform.access import router as mgr
    from src.platform.access.public_router import router

    app = _base_app()
    app.include_router(router, prefix="/api/v1")

    monkeypatch.setattr(mgr, "_get_client", lambda: _FakeConnClient(_conn_row()))
    # Caller IS a member — the point is the payload is masked even so.
    monkeypatch.setattr(mgr, "_require_connection_project_access", lambda *a, **k: None)

    with TestClient(app) as tc:
        r = tc.get("/api/v1/access/surfaces/conn-1")
    assert r.status_code == 200, r.text
    conn = r.json()["data"]
    assert "access_key" not in conn
    assert "mcp_api_key" not in (conn.get("config") or {})
    assert "mcpkey_abcd1234WXYZ" not in r.text


# ── Bug 6: get_connection_run scoping ────────────────────────────────

def test_get_connection_run_foreign_project_forbidden():
    from src.platform.synchronize import router as integ
    from src.platform.synchronize.public_router import router as integ_router

    app = _base_app()
    app.include_router(integ_router, prefix="/api/v1")

    svc = MagicMock()
    # connection exists and belongs to a project the caller can't reach
    svc.repository.get_by_id.return_value = SimpleNamespace(id="conn-1", project_id=FOREIGN)
    from src.platform.synchronize.dependencies import get_synchronize_service
    app.dependency_overrides[get_synchronize_service] = lambda: svc

    run = SimpleNamespace(
        id="run-1", synchronize_binding_id="conn-1", status="ok", worker_job_id=None,
        started_at=None, finished_at=None, duration_ms=None, exit_code=None,
        stdout=None, error=None, trigger_type=None, result_summary=None,
    )
    with patch.object(integ, "_get_run_repo") as run_repo:
        run_repo.return_value.get_by_id.return_value = run
        with TestClient(app) as tc:
            r = tc.get("/api/v1/synchronize/runs/run-1")
    assert r.status_code == 404, r.text


# ── Bug 5: internal table endpoints acting-user access ───────────────

def _fake_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_internal_table_access_rejects_non_member():
    from src.internal.router import _enforce_acting_user_table_access

    req = _fake_request({"x-acting-user-id": "intruder"})
    table_service = MagicMock()
    table_service.get_by_id.return_value = SimpleNamespace(project_id="project-x")

    with patch(
        "src.platform.authorization.factory.build_authorization_service",
        return_value=authorization_for(),
    ):
        with pytest.raises(HTTPException) as exc:
            _enforce_acting_user_table_access(req, table_service, "table-1")
    assert exc.value.status_code == 403


def test_internal_table_access_missing_acting_user_400():
    from src.internal.router import _enforce_acting_user_table_access

    req = _fake_request({})  # no acting-user header
    table_service = MagicMock()
    table_service.get_by_id.return_value = SimpleNamespace(project_id="project-x")
    with pytest.raises(HTTPException) as exc:
        _enforce_acting_user_table_access(req, table_service, "table-1")
    assert exc.value.status_code == 400


def test_internal_table_access_allows_member():
    from src.internal.router import _enforce_acting_user_table_access

    req = _fake_request({"x-acting-user-id": "alice"})
    table_service = MagicMock()
    table_service.get_by_id.return_value = SimpleNamespace(project_id="project-x")
    with patch(
        "src.platform.authorization.factory.build_authorization_service",
        return_value=authorization_for("project-x"),
    ):
        result = _enforce_acting_user_table_access(req, table_service, "table-1")
    assert result == "alice"
