"""Initialization metadata CAS/lease safety; no claim of remote object health."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import Authority
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_publication_pins import rpc

pytestmark = pytest.mark.hosting_live


def initialize(pg, project, lease=None, holder=None, check=True):
    return rpc(pg, 'initialize_legacy_version_project_root', project, lease, holder, check=check)


def lease_for(pg, project):
    lease, holder = str(uuid.uuid4()), 'root-initializer'
    rpc(pg, 'acquire_project_write_lease', project, lease, holder, 'test-init', 120)
    return lease, holder


@pytest.mark.parametrize('lifecycle', ['ready', 'initializing'])
def test_checked_root_initialization_requires_lease_and_preserves_existing_roots(pg_project, lifecycle):
    pg, project = pg_project
    assert initialize(pg, project).stdout.strip() == '1'*40
    pg.sql(f'UPDATE public.projects SET version_root_hash=NULL WHERE id={literal(project)}')
    denied = initialize(pg, project, check=False)
    assert denied.returncode and 'repository_write_lease_unavailable' in denied.stderr
    lease, holder = lease_for(pg, project)
    pg.sql(f'UPDATE public.projects SET lifecycle_status={literal(lifecycle)} WHERE id={literal(project)}')
    assert initialize(pg, project, lease, holder).stdout.strip() == EMPTY_TREE_SHA1
    assert initialize(pg, project).stdout.strip() == EMPTY_TREE_SHA1
    assert pg.value(f'SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}') == '0'


def test_checked_root_initialization_missing_root_with_ack_is_not_unborn(pg_project):
    pg, project = pg_project
    pg.sql(pg.publish(project, '1'*40, '2'*40, 'a'*40))
    assert initialize(pg, project).stdout.strip() == '2'*40
    pg.sql(f'UPDATE public.projects SET version_root_hash=NULL WHERE id={literal(project)}')
    denied = initialize(pg, project, check=False)
    assert denied.returncode and 'legacy_repository_root_corrupt' in denied.stderr
    assert pg.value(f'SELECT version_root_hash IS NULL FROM public.projects WHERE id={literal(project)}') == 't'
    assert pg.value(f'SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}') == '1'


def test_checked_root_initialization_never_changes_native_authority(pg_project):
    pg, project = pg_project
    a = Authority(pg, project)
    denied = initialize(pg, project, check=False)
    assert denied.returncode and 'native_repository_initialization_required' in denied.stderr
    assert pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}') == '1'*40
    assert a.count('version_ref_transactions') == 0


def test_checked_root_initialization_preserves_publication_winning_lock_race(pg_project):
    pg, project = pg_project
    name = 'initialize-race-'+uuid.uuid4().hex
    query = f'SET application_name={literal(name)}; SET ROLE service_role; SELECT public.initialize_legacy_version_project_root({literal(project)},NULL,NULL)'
    with ThreadPoolExecutor() as pool:
        with transaction(pg, f'SELECT 1 FROM public.projects WHERE id={literal(project)} FOR UPDATE') as writer:
            written = json.loads(writer.execute(pg.publish(project, '1'*40, '2'*40, 'a'*40)))
            assert written['published'] is True
            pending = pool.submit(pg.sql, query)
            wait_for_lock(pg, name)
            writer.execute('COMMIT')
        assert pending.result(timeout=10).stdout.strip() == '2'*40
    assert pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}') == '2'*40
    assert pg.value(f'SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}') == '1'


def test_checked_root_initialization_lease_expiry_after_wait_leaves_absence(pg_project):
    pg, project = pg_project
    pg.sql(f'UPDATE public.projects SET version_root_hash=NULL WHERE id={literal(project)}')
    lease, holder = lease_for(pg, project)
    pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(lease)}")
    name = 'initialize-expiry-'+uuid.uuid4().hex
    query = f'SET application_name={literal(name)}; SET ROLE service_role; SELECT public.initialize_legacy_version_project_root('+','.join(map(literal, (project, lease, holder)))+')'
    with ThreadPoolExecutor() as pool:
        with transaction(pg, f'SELECT 1 FROM public.projects WHERE id={literal(project)} FOR UPDATE'):
            pending = pool.submit(pg.sql, query, check=False)
            wait_for_lock(pg, name)
            time.sleep(2.1)
        denied = pending.result(timeout=10)
    assert denied.returncode and 'repository_write_lease_unavailable' in denied.stderr
    assert pg.value(f'SELECT version_root_hash IS NULL FROM public.projects WHERE id={literal(project)}') == 't'


@pytest.mark.parametrize('root', ['0'*40, 'A'*40, 'a'*64, 'not-an-oid'])
def test_checked_root_initialization_rejects_corrupt_root_without_repair(pg_project, root):
    pg, project = pg_project
    pg.sql(f'UPDATE public.projects SET version_root_hash={literal(root)} WHERE id={literal(project)}')
    denied = initialize(pg, project, check=False)
    assert denied.returncode and 'legacy_repository_root_corrupt' in denied.stderr
    assert pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}') == root


def test_checked_root_initialization_rejects_deleting_project(pg_project):
    pg, project = pg_project
    pg.sql(f"UPDATE public.projects SET lifecycle_status='deleting' WHERE id={literal(project)}")
    denied = initialize(pg, project, check=False)
    assert denied.returncode and 'repository_unavailable' in denied.stderr
    assert pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}') == '1'*40


def test_checked_root_initialization_acl(pg_project):
    pg, _ = pg_project
    for role in ('anon', 'authenticated', 'service_role'):
        assert pg.value(f"SELECT has_function_privilege({literal(role)},'public.initialize_legacy_version_project_root(text,uuid,text)','EXECUTE')") == ('t' if role == 'service_role' else 'f')
    assert pg.value("SELECT proconfig::text FROM pg_proc WHERE oid='public.initialize_legacy_version_project_root(text,uuid,text)'::regprocedure") == '{"search_path=pg_catalog, public, pg_temp"}'
