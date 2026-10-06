"""Current stored authorization/lease facts, not end-user token authentication."""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import Authority, symbolic, update
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock

pytestmark = pytest.mark.hosting_live


def seed_legacy_scope(pg, project):
    """Negative fixture: a bounded legacy view predates synthetic enrollment.

    This is not a supported migration. Native creation now rejects new Scope
    configuration; the admission tests must still prove that even a persisted
    bounded credential cannot be widened by an owner-installed authority row.
    """
    scope_id = 'scope-' + uuid.uuid4().hex
    pg.sql(f"INSERT INTO public.repository_scopes(id,project_id,name,path,max_mode) "
           f"VALUES({literal(scope_id)},{literal(project)},'private','private','rw')")
    return scope_id


@pytest.fixture
def admission(pg_project):
    pg, project = pg_project
    scope_id = seed_legacy_scope(pg, project)
    authority = Authority(pg, project)
    org = pg.value(f"SELECT org_id FROM public.projects WHERE id={literal(project)}")
    user, lease = str(uuid.uuid4()), str(uuid.uuid4())
    pg.sql(f"""
        INSERT INTO auth.users(id,aud,role,email,encrypted_password,email_confirmed_at,raw_app_meta_data,raw_user_meta_data,created_at,updated_at)
        VALUES({literal(user)},'authenticated','authenticated',{literal(user+'@example.test')},'',now(),'{{}}','{{}}',now(),now());
        INSERT INTO public.org_members(id,org_id,user_id,role)
        VALUES({literal('om-'+user)},{literal(org)},{literal(user)},'member');
        INSERT INTO public.project_members(id,org_id,project_id,user_id,role)
        VALUES({literal('pm-'+user)},{literal(org)},{literal(project)},{literal(user)},'editor');
        SELECT public.acquire_project_write_lease({literal(project)},{literal(lease)},'native-test-holder','native-test',120);
    """)
    return SimpleNamespace(pg=pg, authority=authority, project=project, org=org, user=user,
                           actor='user:'+user, lease=lease, holder='native-test-holder',
                           scope_id=scope_id)


def check_query(a, actor=None, *, lease=None, holder=None):
    return "SELECT public.check_version_repository_write_admission(" + ",".join(map(literal, (
        a.project, actor or a.actor, lease or a.lease, holder or a.holder))) + ")"


def apply_query(a, *, actor=None, key=None):
    args = a.authority.parameters(
        [update(b"HEAD", symbolic(b"refs/heads/main"), symbolic(b"refs/heads/topic"))],
        actor=actor or a.actor, key=key, receipt=False,
    )
    return "SELECT public.apply_admitted_version_ref_transaction(" + ",".join(map(literal, (
        *args.values(), a.lease, a.holder))) + ")"


def seed_credential(a, *, scope=False):
    surface, credential = 'surface-'+uuid.uuid4().hex, 'credential-'+uuid.uuid4().hex
    scope_id = a.scope_id if scope else None
    a.pg.sql(f"""
        INSERT INTO public.access_surfaces(id,org_id,project_id,scope_id,kind,name,config,status)
        VALUES({literal(surface)},{literal(a.org)},{literal(a.project)},{literal(scope_id)},'git_remote','test','{{"mode":"rw"}}','active');
        INSERT INTO public.access_surface_credentials(id,org_id,project_id,access_surface_id,credential_type,key_prefix,key_last4,key_hash,grant_mode,user_id,credential_lifecycle,status)
        VALUES({literal(credential)},{literal(a.org)},{literal(a.project)},{literal(surface)},'git_http_token','test-key','test',
               {literal(uuid.uuid4().hex * 2)},'rw',{literal(a.user)},'user','active');
    """)
    return surface, credential, 'runtime:'+credential


def test_current_editor_and_root_credential_can_enter_admission(admission):
    a = admission
    for actor in (a.actor, seed_credential(a)[2]):
        result = json.loads(a.pg.value("SET ROLE service_role;" + check_query(a, actor)))
        assert result['project_id'] == a.project and result['object_format'] == 'sha1'
    assert a.authority.count('version_ref_transactions') == 0


@pytest.mark.parametrize('change', ['viewer', 'project_removed', 'org_removed', 'invalid_actor'])
def test_stale_human_grant_cannot_publish(admission, change):
    a = admission
    if change == 'viewer':
        a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}")
    elif change == 'project_removed':
        a.pg.sql(f"DELETE FROM public.project_members WHERE user_id={literal(a.user)}")
    elif change == 'org_removed':
        a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
    else:
        a.actor = 'user:not-a-uuid'
    result = a.pg.sql("SET ROLE service_role;" + apply_query(a), check=False)
    assert result.returncode and 'repository_action_denied' in result.stderr
    assert a.authority.count('version_ref_transactions') == 0
    assert a.authority.state(b'HEAD')['target'] == b'refs/heads/main'.hex()


@pytest.mark.parametrize('change', ['scope', 'revoked', 'expired', 'read_only', 'surface_read_only', 'surface_inactive', 'viewer', 'org_removed'])
def test_current_credential_restrictions_are_rechecked(admission, change):
    a = admission
    surface, credential, actor = seed_credential(a, scope=change == 'scope')
    changes = {
        'revoked': f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(credential)}",
        'expired': f"UPDATE public.access_surface_credentials SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(credential)}",
        'read_only': f"UPDATE public.access_surface_credentials SET grant_mode='r' WHERE id={literal(credential)}",
        'surface_read_only': f"UPDATE public.access_surfaces SET config='{{\"mode\":\"r\"}}' WHERE id={literal(surface)}",
        'surface_inactive': f"UPDATE public.access_surfaces SET status='disabled' WHERE id={literal(surface)}",
        'viewer': f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}",
        'org_removed': f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}",
    }
    if change in changes:
        a.pg.sql(changes[change])
    result = a.pg.sql("SET ROLE service_role;" + apply_query(a, actor=actor), check=False)
    assert result.returncode and 'repository_action_denied' in result.stderr
    assert a.authority.count('version_ref_transactions') == 0


@pytest.mark.parametrize('change', ['missing', 'holder', 'expired', 'released', 'foreign'])
def test_current_write_lease_is_required_at_publication(admission, change):
    a = admission
    if change == 'missing':
        a.lease = str(uuid.uuid4())
    elif change == 'holder':
        a.holder = 'different-holder'
    elif change == 'expired':
        a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(a.lease)}")
    elif change == 'released':
        a.pg.sql(f"SELECT public.release_project_write_lease({literal(a.lease)},{literal(a.holder)})")
    else:
        other = a.pg.create_project()
        a.pg.sql(f"UPDATE public.project_write_leases SET project_id={literal(other)} WHERE id={literal(a.lease)}")
    result = a.pg.sql("SET ROLE service_role;" + apply_query(a), check=False)
    assert result.returncode and 'repository_write_lease_unavailable' in result.stderr
    assert a.authority.count('version_ref_transactions') == 0


def test_committed_replay_requires_current_read_access_but_not_old_write_lease(admission):
    a, key = admission, str(uuid.uuid4())
    statement = "SET ROLE service_role;" + apply_query(a, key=key)
    first = json.loads(a.pg.value(statement))
    assert first['status'] == 'committed'
    a.pg.sql(f"""
        UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)};
        DELETE FROM public.project_write_leases WHERE id={literal(a.lease)};
        UPDATE public.version_repositories SET generation=generation+1,write_state='fenced' WHERE project_id={literal(a.project)};
    """)
    assert json.loads(a.pg.value(statement)) == first
    changed = a.pg.sql(statement.replace('SQL fixture', 'changed message'), check=False)
    assert changed.returncode and 'request_key_reused' in changed.stderr
    a.pg.sql(f"DELETE FROM public.project_members WHERE user_id={literal(a.user)}")
    # Org-visible Projects retain the canonical Viewer baseline.
    assert json.loads(a.pg.value(statement)) == first
    a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
    result = a.pg.sql(statement, check=False)
    assert result.returncode and 'repository_action_denied' in result.stderr
    assert a.authority.count('version_ref_transactions') == 1


@pytest.mark.parametrize('expiring', ['credential', 'lease'])
@pytest.mark.parametrize('entry', ['check', 'apply'])
def test_expiry_after_repository_lock_wait_cannot_publish(admission, expiring, entry):
    a = admission
    _surface, credential, actor = seed_credential(a)
    table, identity = ('access_surface_credentials', credential) if expiring == 'credential' else ('project_write_leases', a.lease)
    name = 'admission-expiry-' + uuid.uuid4().hex
    query = check_query(a, actor) if entry == 'check' else apply_query(a, actor=actor)
    a.pg.sql(f"UPDATE public.{table} SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(identity)}")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(a.pg, f"SELECT 1 FROM public.version_repositories WHERE project_id={literal(a.project)} FOR UPDATE") as session:
            pending = pool.submit(a.pg.sql, f"SET application_name={literal(name)};SET ROLE service_role;" + query, check=False)
            wait_for_lock(a.pg, name)
            deadline = time.monotonic() + 5
            while a.pg.value(f"SELECT expires_at<=clock_timestamp() FROM public.{table} WHERE id={literal(identity)}") != 't':
                assert time.monotonic() < deadline, 'expiry barrier timed out'
                time.sleep(0.02)
            session.execute('COMMIT')
        result = pending.result(timeout=10)
    expected = 'repository_action_denied' if expiring == 'credential' else 'repository_write_lease_unavailable'
    assert result.returncode and expected in result.stderr
    for table in ('version_ref_transactions', 'version_reflog_entries', 'version_ref_events'):
        assert a.authority.count(table) == 0
    assert a.pg.value(f"SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(a.project)}") == '0'
    assert a.authority.state(b'HEAD')['target'] == b'refs/heads/main'.hex()


def test_write_admission_rpc_and_helper_privileges(admission):
    a = admission
    private = [
        '_version_lock_admission_project(text)',
        '_version_assert_current_repository_actor(text,text,text,boolean)',
        '_version_assert_write_lease(text,uuid,text)',
    ]
    public = [
        'check_version_repository_write_admission(text,text,uuid,text)',
        'apply_admitted_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text)',
    ]
    for signature in private + public:
        for role in ('anon', 'authenticated', 'service_role'):
            actual = a.pg.value(f"SELECT has_function_privilege({literal(role)},{literal('public.'+signature)},'EXECUTE')")
            assert actual == ('t' if role == 'service_role' and signature in public else 'f')
        config = a.pg.value(f"SELECT proconfig::text FROM pg_proc WHERE oid={literal('public.'+signature)}::regprocedure")
        assert 'pg_catalog, public, pg_temp' in config


@pytest.mark.parametrize('fact', ['project_member', 'org_member', 'credential', 'surface'])
@pytest.mark.parametrize('order', ['publication_first', 'revocation_first'])
def test_revocation_and_publication_have_an_observed_serial_order(admission, fact, order):
    a = admission
    surface, credential, actor = seed_credential(a)
    mutations = {
        'project_member': f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}",
        'org_member': f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}",
        'credential': f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(credential)}",
        'surface': f"UPDATE public.access_surfaces SET status='disabled' WHERE id={literal(surface)}",
    }
    name = 'native-admission-' + uuid.uuid4().hex
    start = f"SET application_name={literal(name)};"
    with ThreadPoolExecutor(max_workers=1) as pool:
        if order == 'publication_first':
            with transaction(a.pg, 'SET ROLE service_role;' + check_query(a, actor)) as session:
                pending = pool.submit(a.pg.sql, start + mutations[fact])
                wait_for_lock(a.pg, name)
                assert not pending.done()
                result = json.loads(session.execute(apply_query(a, actor=actor)))
                assert result['status'] == 'committed'
                session.execute('COMMIT')
            assert pending.result(timeout=10).returncode == 0
            assert a.authority.count('version_ref_transactions') == 1
        else:
            with transaction(a.pg, mutations[fact]) as session:
                pending = pool.submit(a.pg.sql, start + 'SET ROLE service_role;' + apply_query(a, actor=actor), check=False)
                wait_for_lock(a.pg, name)
                assert not pending.done()
                session.execute('COMMIT')
            result = pending.result(timeout=10)
            assert result.returncode and 'repository_action_denied' in result.stderr
            assert a.authority.count('version_ref_transactions') == 0
