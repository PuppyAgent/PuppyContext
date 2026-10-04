"""Production initializer over actual SDK/PostgREST, with synthetic root metadata."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.infrastructure.supabase.history_repository import SupabaseHistoryManager
from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_checked_publish_api import api as api_fixture

pytestmark = pytest.mark.hosting_supabase
api = api_fixture


@pytest.mark.asyncio
async def test_checked_root_initialization_real_sdk_never_resets_ack(api, pg_project):
    pg, project = pg_project
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
        sdk = create_client(str(api.client.base_url), api.headers('service_role')['apikey'],
                            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False))
        history = SupabaseHistoryManager(SimpleNamespace(client=sdk), project)
        assert await asyncio.to_thread(history.initialize_root_hash) == '1'*40
        assert history.get_root_hash() == '1'*40
        pg.sql(f'UPDATE public.projects SET version_root_hash=NULL WHERE id={literal(project)}')
        async with ProjectWriteLease(project, 'checked-init', repository=ProjectWriteLeaseRepository(sdk)):
            assert await asyncio.to_thread(history.initialize_root_hash) == EMPTY_TREE_SHA1
        assert history.get_root_hash() == EMPTY_TREE_SHA1  # Old local metadata cache was invalidated.
        pg.sql(pg.publish(project, EMPTY_TREE_SHA1, '2'*40, 'a'*40))
        assert await asyncio.to_thread(history.initialize_root_hash) == '2'*40
        assert history.get_root_hash() == '2'*40
        assert pg.value(f'SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}') == '1'


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
def test_checked_root_initialization_rejects_end_user_rpc(api, pg_project, role):
    pg, project = pg_project
    response = api.request('POST', '/rest/v1/rpc/initialize_legacy_version_project_root', role=role,
                           json={'p_project_id': project, 'p_lease_id': None, 'p_holder_id': None})
    assert response.status_code in (401, 403, 404)
    assert response.json()['code'] in ('42501', 'PGRST202')
    assert pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}') == '1'*40
