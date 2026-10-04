"""Actual SDK/PostgREST renewal and client ACLs; actor fixtures are synthetic."""
import uuid

import httpx
import pytest
from postgrest.exceptions import APIError
from supabase import ClientOptions, create_client

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_checked_publish_api import api as api_fixture
from tests.repository_hosting.integration.test_repository_write_admission import (
    admission as admission_fixture,
)
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential

pytestmark = pytest.mark.hosting_supabase
api, admission = api_fixture, admission_fixture


def test_admitted_pin_renewal_real_sdk_and_revocation(api, admission):
    a = admission
    _, credential, actor = seed_credential(a)
    pin = str(uuid.uuid4())
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
        sdk = create_client(str(api.client.base_url), api.headers('service_role')['apikey'],
                            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False))
        control = AdmittedRefAuthorityRepository(sdk, lease_provider=lambda _: None)
        control.begin_read(a.project, actor, pin)
        assert isinstance(control.renew(a.project, actor, pin), str)
        before = a.pg.value(f'SELECT expires_at FROM public.version_object_pins WHERE id={literal(pin)}')
        a.pg.sql(f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(credential)}")
        with pytest.raises(APIError, match='repository_action_denied'):
            control.renew(a.project, actor, pin)
        assert a.pg.value(f'SELECT expires_at FROM public.version_object_pins WHERE id={literal(pin)}') == before


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
def test_admitted_pin_renewal_client_rpc_denied(api, admission, role):
    a = admission
    response = api.request('POST', '/rest/v1/rpc/renew_admitted_version_object_pin', role=role,
                           json={'p_project_id': a.project, 'p_actor': a.actor, 'p_pin_id': str(uuid.uuid4())})
    assert response.status_code in (401, 403, 404)
    assert response.json()['code'] in ('42501', 'PGRST202')
