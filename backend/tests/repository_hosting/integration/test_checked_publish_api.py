"""Production history adapter -> real SDK -> owned PostgREST -> repaired SQL.

Actual JWT boundary, synthetic metadata/OIDs; not object durability evidence.
"""

import os
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.version_engine.infrastructure.supabase.history_repository import SupabaseHistoryManager
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.supabase_api import SupabaseAPI
from tests.repository_hosting.unit.test_checked_publish_selection import publish_args

pytestmark = pytest.mark.hosting_supabase


@pytest.fixture(scope="module")
def api():
    owned = SupabaseAPI(os.environ)  # Validates ownership/loopback before any I/O.
    try:
        owned.authenticate()
        yield owned
    finally:
        owned.close()


@pytest.mark.parametrize("metered", [False, True])
@pytest.mark.parametrize("scope", ["", "docs"])
def test_production_adapter_checked_cas_over_real_postgrest(api, pg_project, metered, scope):
    pg, project = pg_project
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
        sdk = create_client(
            str(api.client.base_url), api.headers("service_role")["apikey"],
            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
        )
        manager = SupabaseHistoryManager(SimpleNamespace(client=sdk), project)
        org = pg.value(f"SELECT org_id FROM public.projects WHERE id={literal(project)}")
        measurement = {"org_id": org, "old_value": 0, "delta": 0, "limit": None, "enforce": False}
        kwargs = publish_args() | {
            "scope_path": scope, "expected_scope_head_commit_id": "",
            "storage_measurement": measurement if metered else None,
        }
        accepted, transaction = manager.publish_project_update(**kwargs)
        assert accepted and transaction is not None
        assert manager.publish_project_update(**(kwargs | {"commit_id": "b" * 40})) == (False, None)
        assert manager.publish_project_update(**(kwargs | {
            "commit_id": "b" * 40, "expected_scope_head_commit_id": "a" * 40,
        }))[0]
        for table in ("version_commits", "version_transactions", "version_outbox"):
            assert pg.value(f"SELECT count(*) FROM public.{table} WHERE project_id={literal(project)}") == "2"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("function", [
    "publish_version_project_update_checked", "publish_version_project_update_with_usage_checked",
])
def test_user_jwt_cannot_call_checked_backend_publish(api, pg_project, role, function):
    pg, project = pg_project
    body = {
        "p_project_id": project, "p_old_root_hash": "1" * 40, "p_new_root_hash": "1" * 40,
        "p_head_commit_id": "a" * 40, "p_who": "test:forged", "p_message": "forbidden",
        "p_event_type": "write", "p_changes": [], "p_conflicts": None, "p_created_at": "",
        "p_audit_agent_id": "test:forged", "p_audit_detail": {}, "p_expected_scope_head_commit_id": "",
    }
    response = api.request("POST", "/rest/v1/rpc/" + function, role=role, json=body)
    assert response.status_code in (401, 403, 404)
    assert response.json()["code"] in ("42501", "PGRST202")
    assert pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}") == "0"
