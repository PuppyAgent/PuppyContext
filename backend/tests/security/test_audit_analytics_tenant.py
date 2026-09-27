"""ISSUE-001 — analytics endpoints must force auth + server-side tenant scoping.

Before the fix, /api/v1/analytics/access-timeseries and /access-summary used
optional auth (anonymous allowed) and queried access_logs with NO project/org
predicate — any anonymous caller could read every tenant's access events.

Hermetic: get_current_user is overridden, and the Supabase seam is faked (a
recording stub), so no DB client is constructed and nothing touches the cloud.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.analytics import router as analytics_router_mod
from src.platform.analytics.router import router as analytics_router
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from tests.authorization_fakes import authorization_for, install_authorization

ALLOWED = "proj-allowed"
FOREIGN = "proj-foreign"


class _RecordingClient:
    """Chainable Supabase stub that records .eq() filters and returns no rows."""

    def __init__(self):
        self.calls: list[tuple] = []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def rpc(self, name, params):
        self.calls.append(("rpc", name, params))
        return self

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.calls.append(("eq", col, val))
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


def _install(monkeypatch, *, member_of):
    """Wire the Supabase analytics seam. Returns the recording client."""
    client = _RecordingClient()
    monkeypatch.setattr(analytics_router_mod, "get_supabase_client", lambda: client)
    return client


def _app(authed: bool, member_of=()):
    app = FastAPI()
    app.include_router(analytics_router)
    install_authorization(app, authorization_for(*member_of))
    if authed:
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id="user-alice",
            email="a@example.com",
            role="authenticated",
        )
    return app


def test_anonymous_is_rejected_401(monkeypatch):
    _install(monkeypatch, member_of={ALLOWED})
    # No get_current_user override → real dependency runs; SKIP_AUTH disabled by
    # the security conftest, so a missing bearer token must 401.
    with TestClient(_app(authed=False, member_of={ALLOWED})) as client:
        r = client.get(f"/api/v1/analytics/access-timeseries?project_id={ALLOWED}")
    assert r.status_code == 401, r.text


def test_missing_project_id_is_422(monkeypatch):
    _install(monkeypatch, member_of={ALLOWED})
    with TestClient(_app(authed=True, member_of={ALLOWED})) as client:
        r = client.get("/api/v1/analytics/access-timeseries")
    assert r.status_code == 422, r.text


def test_non_member_is_403_and_no_query(monkeypatch):
    client = _install(monkeypatch, member_of=set())  # member of nothing
    with TestClient(_app(authed=True, member_of=set())) as tc:
        r = tc.get(f"/api/v1/analytics/access-timeseries?project_id={FOREIGN}")
    assert r.status_code == 403, r.text
    # CRITICAL: access_logs must NOT have been queried before the 403.
    assert not any(call[0] in {"table", "rpc"} for call in client.calls)


def test_member_query_is_scoped_to_project(monkeypatch):
    client = _install(monkeypatch, member_of={ALLOWED})
    with TestClient(_app(authed=True, member_of={ALLOWED})) as tc:
        r = tc.get(f"/api/v1/analytics/access-timeseries?project_id={ALLOWED}")
    assert r.status_code == 200, r.text
    rpc = next(call for call in client.calls if call[:2] == ("rpc", "analytics_access_timeseries"))
    assert rpc[2]["p_project_id"] == ALLOWED


def test_summary_also_scoped_and_authenticated(monkeypatch):
    client = _install(monkeypatch, member_of={ALLOWED})
    with TestClient(_app(authed=True, member_of={ALLOWED})) as tc:
        r = tc.get(f"/api/v1/analytics/access-summary?project_id={ALLOWED}")
    assert r.status_code == 200, r.text
    rpc = next(call for call in client.calls if call[:2] == ("rpc", "analytics_access_summary"))
    assert rpc[2]["p_project_id"] == ALLOWED

    # And a non-member is blocked on the summary endpoint too.
    client2 = _install(monkeypatch, member_of=set())
    with TestClient(_app(authed=True, member_of=set())) as tc:
        r2 = tc.get(f"/api/v1/analytics/access-summary?project_id={FOREIGN}")
    assert r2.status_code == 403, r2.text
    assert not any(call[0] in {"table", "rpc"} for call in client2.calls)


def test_analytics_migration_has_composite_index_rls_and_private_rpcs():
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase/archive/before_b1/migrations/20260711030000_harden_analytics_aggregation.sql"
    )
    sql = migration.read_text(encoding="utf-8")
    assert "ON public.access_logs (project_id, created_at DESC)" in sql
    assert "CREATE POLICY access_logs_authenticated_project_member" in sql
    assert "analytics_access_timeseries" in sql
    assert "analytics_access_summary" in sql
    assert "FROM PUBLIC, anon, authenticated" in sql
