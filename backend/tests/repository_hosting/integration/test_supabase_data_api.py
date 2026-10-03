"""Actual local Auth + PostgREST boundary; metadata/receipts remain owner fixtures.

Not admitted application authorization, live migration or S3 durability evidence.
"""

import os
import uuid

import pytest

from tests.repository_hosting.harness.ref_authority import (
    TABLES,
    A,
    Authority,
    B,
    C,
    oid,
    symbolic,
    update,
)
from tests.repository_hosting.harness.supabase_api import SupabaseAPI

pytestmark = pytest.mark.hosting_supabase


@pytest.fixture(scope="module")
def api():
    api = SupabaseAPI(os.environ)
    try:
        api.authenticate()
        yield api
    finally:
        api.close()


@pytest.fixture
def authority(pg_project):
    a = Authority(*pg_project)
    assert a.apply([update(new=oid(A))])["status"] == "committed"
    return a


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("table", TABLES)
def test_clients_cannot_read_authority_tables(api, authority, role, table):
    response = api.request("GET", f"/rest/v1/{table}", role=role,
                           params={"select": "project_id", "project_id": f"eq.{authority.project}"})
    assert response.status_code in (401, 403)
    assert response.json()["code"] == "42501"


@pytest.mark.parametrize("table", TABLES)
def test_backend_can_read_authority_tables(api, authority, table):
    response = api.request("GET", f"/rest/v1/{table}",
                           params={"select": "project_id", "project_id": f"eq.{authority.project}"})
    assert response.status_code == 200
    rows = response.json()
    assert rows and all(row["project_id"] == authority.project for row in rows)


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("table", TABLES)
def test_backend_cannot_directly_mutate_authority_or_issue_receipts(api, authority, method, table):
    response = api.request(method, f"/rest/v1/{table}",
                           params={"project_id": f"eq.{authority.project}"},
                           **({"json": {"project_id": authority.project}} if method != "DELETE" else {}))
    assert response.status_code == 403
    assert response.json()["code"] == "42501"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("function", ["apply_version_ref_transaction", "get_version_ref_transaction"])
def test_clients_cannot_call_backend_ref_rpcs(api, authority, role, function):
    body = authority.parameters([update(old=oid(A), new=oid(B))])
    if function.startswith("get_"):
        body = {key: body[key] for key in ("p_project_id", "p_actor", "p_request_key")}
    response = api.request("POST", f"/rest/v1/rpc/{function}", role=role, json=body)
    assert response.status_code in (401, 403, 404)
    assert response.json()["code"] in ("42501", "PGRST202")
    assert authority.state()["oid"] == A


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("function", [
    "register_version_object_locations", "remove_version_object_locations", "authorize_version_object_deletion",
])
def test_current_auth_clients_cannot_call_storage_coordination(api, authority, role, function):
    body = {"p_project_id": authority.project}
    if function == "register_version_object_locations":
        body.update(p_actor="test:writer", p_pin_id=str(uuid.uuid4()), p_rows=[])
    else:
        body["p_gc_token"] = str(uuid.uuid4())
        if function == "remove_version_object_locations":
            body["p_object_ids"] = [A]
    response = api.request("POST", f"/rest/v1/rpc/{function}", role=role, json=body)
    assert response.status_code in (401, 403, 404)
    assert response.json()["code"] in ("42501", "PGRST202")
    assert authority.state()["oid"] == A


def rpc(api, body, function="apply_version_ref_transaction"):
    response = api.request("POST", f"/rest/v1/rpc/{function}", json=body)
    assert response.status_code == 200
    return response.json()


def test_backend_rpc_commit_replay_and_actor_bound_result_query(api, authority):
    a = authority
    body = a.parameters([update(old=oid(A), new=oid(B))], actor="test:http")
    result = rpc(api, body)
    assert result["status"] == "committed"
    assert rpc(api, body) == result
    query = {key: body[key] for key in ("p_project_id", "p_actor", "p_request_key")}
    assert rpc(api, query, "get_version_ref_transaction") == result
    assert rpc(api, query | {"p_actor": "test:other"}, "get_version_ref_transaction") is None
    assert rpc(api, query | {"p_request_key": str(uuid.uuid4())}, "get_version_ref_transaction") is None
    assert a.state()["oid"] == B
    assert a.count("version_ref_transactions") == a.count("version_ref_events") == 2


def test_backend_rpc_rejected_result_survives_later_publication(api, authority):
    a = authority
    body = a.parameters([update(new=oid(B))])  # Expected absent, but A already exists.
    rejected = rpc(api, body)
    assert rejected["status"] == "rejected" and rejected["reason"] == "stale_ref"
    assert rpc(api, a.parameters([update(old=oid(A), new=oid(C))]))["status"] == "committed"
    assert rpc(api, body) == rejected
    changed = body | {"p_updates": [update(old=oid(C), new=oid(B))]}
    response = api.request("POST", "/rest/v1/rpc/apply_version_ref_transaction", json=changed)
    assert response.status_code == 400
    assert response.json()["message"] == "request_key_reused"
    assert a.state()["oid"] == C
    assert a.count("version_ref_transactions") == 3
    assert a.count("version_ref_events") == 2


def test_backend_rpc_roundtrips_byte_refs_and_atomic_symbolic_head(api, authority):
    a = authority
    name = b"refs/heads/byte-\xff"
    body = a.parameters([
        update(name, new=oid(B)),
        update(b"HEAD", symbolic(b"refs/heads/main"), symbolic(name)),
    ])
    assert rpc(api, body)["status"] == "committed"
    assert a.state(name)["oid"] == B
    assert a.state(b"HEAD")["target"] == name.hex()
    assert a.count("version_reflog_entries") == 3
    assert a.count("version_ref_events") == 2


def test_postgrest_rejects_invalid_jwt(api):
    response = api.client.get("/rest/v1/version_repositories", headers=api.headers("anon") | {
        "Authorization": "Bearer not-a-jwt",
    })
    assert response.status_code == 401
