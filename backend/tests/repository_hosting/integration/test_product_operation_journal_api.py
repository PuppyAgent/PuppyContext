"""Real SDK/PostgREST journal boundary; stored actors are synthetic fixtures."""

import uuid
from types import SimpleNamespace

import httpx
import pytest
from postgrest.exceptions import APIError
from supabase import ClientOptions, create_client

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_checked_publish_api import api as api_fixture
from tests.repository_hosting.integration.test_product_operation_journal import DIGEST, proposal
from tests.repository_hosting.integration.test_product_operation_journal import (
    journal_actor as journal_fixture,
)

pytestmark = pytest.mark.hosting_supabase
api, journal_actor = api_fixture, journal_fixture


def test_product_operation_journal_real_sdk_replay_and_revocation(api, journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    lease = SimpleNamespace(is_active=True, project_id=a.project, lease_id=a.lease, holder_id=a.holder)
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
        sdk = create_client(str(api.client.base_url), api.headers("service_role")["apikey"],
                            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False))
        control = AdmittedRefAuthorityRepository(sdk, lease_provider=lambda _: lease)
        assert control.operation_status(a.project, a.actor, key) is None
        first = control.begin_product_operation(a.project, a.actor, key, DIGEST, 1)
        assert control.operation_status(a.project, a.actor, key)["status"] == "pending"
        prepared = control.prepare_product_operation(a.project, a.actor, key, DIGEST, proposal())
        assert first["created_at"] == prepared["created_at"]
        assert prepared["proposal"] == proposal() and prepared["result"] is None
        result = a.authority.apply(proposal()["updates"], actor=a.actor, key=key, receipt=False)
        a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)};"
                 f"DELETE FROM public.project_write_leases WHERE id={literal(a.lease)}")
        lease.is_active = False
        assert control.begin_product_operation(a.project, a.actor, key, DIGEST, 1)["result"] == result
        assert control.read_product_operation(a.project, a.actor, key, DIGEST, 1)["result"] == result
        assert control.operation_status(a.project, a.actor, key)["result"] == result
        with pytest.raises(APIError, match="request_key_reused"):
            control.begin_product_operation(a.project, a.actor, key, "2"*64, 1)
        a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
        with pytest.raises(APIError, match="repository_action_denied"):
            control.begin_product_operation(a.project, a.actor, key, DIGEST, 1)
        with pytest.raises(APIError, match="repository_action_denied"):
            control.operation_status(a.project, a.actor, key)
        assert a.authority.count("version_ref_transactions") == 1


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_product_operation_journal_not_a_client_data_api(api, journal_actor, role):
    a = journal_actor
    parameters = {"p_project_id": a.project, "p_actor": a.actor, "p_request_key": str(uuid.uuid4()),
                  "p_input_sha256": DIGEST, "p_lease_id": a.lease, "p_holder_id": a.holder}
    for operation, extra in (("begin", {"p_generation": 1}), ("prepare", {"p_proposal": proposal()})):
        response = api.request("POST", f"/rest/v1/rpc/{operation}_admitted_version_product_operation",
                               role=role, json={**parameters, **extra})
        assert response.status_code in (401, 403, 404)
        assert response.json()["code"] in ("42501", "PGRST202")
    read_parameters = {name: value for name, value in parameters.items() if name not in {"p_lease_id", "p_holder_id"}}
    response = api.request("POST", "/rest/v1/rpc/read_admitted_version_product_operation",
                           role=role, json={**read_parameters, "p_generation": 1})
    assert response.status_code in (401, 403, 404)
    assert response.json()["code"] in ("42501", "PGRST202")
    response = api.request("POST", "/rest/v1/rpc/get_admitted_version_operation_status", role=role,
                           json={name: parameters[name] for name in ("p_project_id", "p_actor", "p_request_key")})
    assert response.status_code in (401, 403, 404)
    assert response.json()["code"] in ("42501", "PGRST202")
    response = api.request("GET", "/rest/v1/version_product_operations?select=*", role=role)
    assert response.status_code in (401, 403)
    assert response.json()["code"] == "42501"
