"""SQL atomic billing with owner-installed receipts; not physical/auth proof."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import (
    ABSENT,
    A,
    Authority,
    B,
    oid,
    symbolic,
    update,
)
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_repository_write_admission import (
    admission as admission_fixture,
)

pytestmark = pytest.mark.hosting_live
admission = admission_fixture


def enroll_billing(a):
    a.pg.sql(f"""
      INSERT INTO public.version_repository_billing(project_id,org_id,initialized)
      VALUES({literal(a.project)},{literal(a.org)},true);
      INSERT INTO public.organization_usage_counters(org_id,metric,value,version)
      VALUES({literal(a.org)},'storage.logical_bytes',0,1)
      ON CONFLICT(org_id,metric) DO UPDATE SET value=0,version=1;
      INSERT INTO public.organization_entitlements(org_id,source,source_revision,payload_hash,entitlements)
      VALUES({literal(a.org)},'puppypay',1,{literal('a'*64)},'{{"limits":{{"storage.max_bytes":10}}}}')
      ON CONFLICT(org_id) DO UPDATE SET source=EXCLUDED.source,source_revision=EXCLUDED.source_revision,
          payload_hash=EXCLUDED.payload_hash,entitlements=EXCLUDED.entitlements;
    """)
    return a


@pytest.fixture
def billing(admission):
    return enroll_billing(admission)


def query(a, edits=None, *, before=None, after=A, old_bytes=0, new_bytes=4, key=None, usage=True):
    arguments = a.authority.parameters(edits or [update(new=oid(after))], actor=a.actor, key=key)
    facts = {'org_id': a.org, 'source_revision': 1, 'old_head_oid': before, 'new_head_oid': after,
             'old_bytes': old_bytes, 'new_bytes': new_bytes} if usage else None
    return 'SELECT public.apply_billed_version_ref_transaction(' + ','.join(map(literal, (
        *arguments.values(), a.lease, a.holder, facts))) + ')'


def value(a):
    return int(a.pg.value(f"SELECT value FROM public.organization_usage_counters WHERE org_id={literal(a.org)} AND metric='storage.logical_bytes'"))


def events(a):
    return int(a.pg.value(f"SELECT count(*) FROM public.organization_usage_events WHERE org_id={literal(a.org)}"))


def test_native_logical_billing_revisits_commit_and_replays_without_recharging(billing):
    a = billing
    key = str(uuid.uuid4())
    first_query = query(a, key=key)
    first = json.loads(a.pg.value('SET ROLE service_role;' + first_query))
    assert first['status'] == 'committed' and value(a) == 4 and events(a) == 1
    for before, after, old, new in [(A, B, 4, 2), (B, A, 2, 4)]:
        result = json.loads(a.pg.value('SET ROLE service_role;' + query(
            a, [update(old=oid(before), new=oid(after))], before=before, after=after, old_bytes=old, new_bytes=new)))
        assert result['status'] == 'committed' and value(a) == new
    assert events(a) == 3
    a.pg.sql(f"""
      UPDATE public.project_members SET role='viewer' WHERE project_id={literal(a.project)} AND user_id={literal(a.user)};
      UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(a.lease)};
      UPDATE public.version_repository_billing SET initialized=false WHERE project_id={literal(a.project)};
      DELETE FROM public.organization_entitlements WHERE org_id={literal(a.org)};
    """)
    assert json.loads(a.pg.value('SET ROLE service_role;' + query(a, key=key, usage=False))) == first
    assert value(a) == 4 and events(a) == 3
    changed = a.pg.sql('SET ROLE service_role;' + query(a, key=key, after=B, usage=False), check=False)
    assert changed.returncode and 'request_key_reused' in changed.stderr


@pytest.mark.parametrize('failure', ['quota', 'revision', 'snapshot', 'missing_usage'])
def test_native_logical_billing_rolls_back_all_publication_effects(billing, failure):
    a = billing
    if failure == 'quota':
        a.pg.sql(f"UPDATE public.organization_entitlements SET entitlements='{{\"limits\":{{\"storage.max_bytes\":3}}}}' WHERE org_id={literal(a.org)}")
    if failure == 'revision':
        a.pg.sql(f"UPDATE public.organization_entitlements SET source_revision=2 WHERE org_id={literal(a.org)}")
    sql = query(a, before=B if failure == 'snapshot' else None, usage=failure != 'missing_usage')
    result = a.pg.sql('SET ROLE service_role;' + sql, check=False)
    expected = {'quota': 'storage_quota_exceeded', 'revision': 'storage_billing_entitlement_changed',
                'snapshot': 'storage_billing_snapshot_changed', 'missing_usage': 'invalid_storage_usage_input'}
    assert result.returncode and expected[failure] in result.stderr
    assert a.authority.state() is None and value(a) == 0 and events(a) == 0
    for table in ('version_ref_transactions', 'version_ref_events', 'version_reflog_entries', 'audit_logs'):
        assert a.authority.count(table) == 0
    assert a.pg.value(f"SELECT count(*) FROM public.version_repository_billing_authorizations WHERE project_id={literal(a.project)}") == '0'
    assert a.pg.value(f"SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(a.project)}") == '0'


def test_native_logical_billing_named_ref_does_not_charge_current_tree(billing):
    a = billing
    result = json.loads(a.pg.value('SET ROLE service_role;' + query(
        a, [update(b'refs/tags/blob', ABSENT, oid('d'*40))])))
    assert result['status'] == 'committed' and value(a) == 0 and events(a) == 0
    assert a.pg.value(f"SELECT payload->'logical_storage'->>'delta_bytes' FROM public.version_ref_events WHERE project_id={literal(a.project)}") == '0'


def test_native_logical_billing_tracks_head_switch_and_default_branch_deletion(billing):
    a = billing
    assert json.loads(a.pg.value('SET ROLE service_role;' + query(a)))['status'] == 'committed'
    branch = b'refs/heads/topic'
    assert json.loads(a.pg.value('SET ROLE service_role;' + query(a, [update(branch, ABSENT, oid(B))])))['status'] == 'committed'
    assert value(a) == 4 and events(a) == 1
    switch = query(a, [update(b'HEAD', symbolic(b'refs/heads/main'), symbolic(branch))],
                   before=A, after=B, old_bytes=4, new_bytes=2)
    assert json.loads(a.pg.value('SET ROLE service_role;' + switch))['status'] == 'committed'
    assert value(a) == 2 and events(a) == 2
    delete = query(a, [update(branch, oid(B), ABSENT)], before=B, after=None, old_bytes=2, new_bytes=0)
    assert json.loads(a.pg.value('SET ROLE service_role;' + delete))['status'] == 'committed'
    assert value(a) == 0 and events(a) == 3


def test_native_logical_billing_acl(billing):
    public = ['check_version_repository_billing(text)',
              'apply_billed_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text,jsonb)']
    private = ['_version_default_head_oid(text)', '_version_billing_entitlement(text,bigint)',
               '_version_lock_billing_organization(text)', '_version_billing_usage(text,text)',
               '_version_fence_billing_publication()', '_version_fence_legacy_storage_reconciliation()']
    for signature in public + private:
        for role in ('anon', 'authenticated', 'service_role'):
            permitted = billing.pg.value(f"SELECT has_function_privilege({literal(role)},{literal('public.'+signature)},'EXECUTE')")
            assert permitted == ('t' if role == 'service_role' and signature in public else 'f')
        assert billing.pg.value(f"SELECT proconfig::text FROM pg_proc WHERE oid={literal('public.'+signature)}::regprocedure") == '{"search_path=pg_catalog, public, pg_temp"}'


def test_native_logical_billing_fences_legacy_full_reconciliation(billing):
    a = billing
    assert json.loads(a.pg.value('SET ROLE service_role;' + query(a)))['status'] == 'committed'
    result = a.pg.sql('SET ROLE service_role; SELECT public.reconcile_organization_usage_counter(' +
                      ','.join(map(literal, (a.org, 'storage.logical_bytes', 0, 10,
                                            'legacy-reconcile-'+uuid.uuid4().hex, 'storage_reconciler', {}))) + ')', check=False)
    assert result.returncode and 'native_storage_reconciliation_required' in result.stderr
    assert value(a) == 4 and events(a) == 1 and a.authority.state()['oid'] == A


def test_native_logical_billing_fences_old_publishers_and_direct_proofs(billing):
    a = billing
    result = a.pg.sql(a.authority.query([update(new=oid(A))]), check=False)
    assert result.returncode and 'repository_billing_coordination_required' in result.stderr
    for table in ('version_repository_billing', 'version_repository_billing_authorizations'):
        result = a.pg.sql(f'SET ROLE service_role; DELETE FROM public.{table}', check=False)
        assert result.returncode and 'permission denied' in result.stderr
    assert a.authority.count('version_ref_transactions') == 0


@pytest.mark.parametrize('failure', ['policy', 'counter', 'entitlement'])
def test_native_logical_billing_preflight_fails_closed(billing, failure):
    a = billing
    changes = {
        'policy': f"UPDATE public.version_repository_billing SET initialized=false WHERE project_id={literal(a.project)}",
        'counter': f"UPDATE public.organization_usage_counters SET version=0 WHERE org_id={literal(a.org)}",
        'entitlement': f"UPDATE public.organization_entitlements SET effective_until=clock_timestamp() WHERE org_id={literal(a.org)}",
    }
    a.pg.sql(changes[failure])
    result = a.pg.sql(f'SET ROLE service_role; SELECT public.check_version_repository_billing({literal(a.project)})', check=False)
    expected = {'policy': 'repository_billing_uninitialized', 'counter': 'storage_billing_usage_uninitialized',
                'entitlement': 'storage_billing_entitlement_invalid'}
    assert result.returncode and expected[failure] in result.stderr


@pytest.mark.parametrize('expiring', ['lease', 'entitlement'])
def test_native_logical_billing_rechecks_expiry_after_its_own_wait(billing, expiring):
    a = billing
    table, column, condition = ('project_write_leases', 'expires_at', f'id={literal(a.lease)}') if expiring == 'lease' else (
        'organization_entitlements', 'effective_until', f'org_id={literal(a.org)}')
    a.pg.sql(f"UPDATE public.{table} SET {column}=clock_timestamp()+interval '2 seconds' WHERE {condition}")
    name = 'logical-billing-wait-' + uuid.uuid4().hex
    with ThreadPoolExecutor(1) as pool:
        with transaction(a.pg, f'SELECT 1 FROM public.organization_entitlements WHERE org_id={literal(a.org)} FOR UPDATE') as session:
            pending = pool.submit(a.pg.sql, f'SET application_name={literal(name)}; SET ROLE service_role;' + query(a), check=False)
            wait_for_lock(a.pg, name)
            deadline = time.monotonic() + 5
            while a.pg.value(f'SELECT {column}<=clock_timestamp() FROM public.{table} WHERE {condition}') != 't':
                assert time.monotonic() < deadline, 'expiry barrier timed out'
                time.sleep(0.02)
            session.execute('COMMIT')
        result = pending.result(timeout=10)
    error = 'repository_write_lease_unavailable' if expiring == 'lease' else 'storage_billing_entitlement_invalid'
    assert result.returncode and error in result.stderr
    assert a.authority.state() is None and a.authority.count('version_ref_transactions') == 0
    assert value(a) == 0 and events(a) == 0


def test_native_logical_billing_serializes_sibling_projects(billing):
    a = billing
    project, lease = str(uuid.uuid4()), str(uuid.uuid4())
    a.pg.sql(f"""
      BEGIN;
      INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status)
      VALUES({literal(project)},'billing-sibling',{literal(a.org)},{literal(a.user)},'ready');
      INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
      VALUES({literal('pm-'+project)},{literal(a.org)},{literal(project)},{literal(a.user)},'admin',{literal(a.user)});
      COMMIT;
      SELECT public.acquire_project_write_lease({literal(project)},{literal(lease)},{literal(a.holder)},'test',120);
    """)
    other = SimpleNamespace(pg=a.pg, project=project, org=a.org, actor=a.actor, holder=a.holder, lease=lease,
                            authority=Authority(a.pg, project))
    a.pg.sql(f'INSERT INTO public.version_repository_billing(project_id,org_id,initialized) '
             f'VALUES({literal(project)},{literal(a.org)},true)')
    gate = Barrier(2)

    def publish(fixture):
        gate.wait(timeout=10)
        return fixture.pg.sql('SET ROLE service_role;' + query(fixture, new_bytes=6), check=False)

    with ThreadPoolExecutor(2) as pool:
        pending = [pool.submit(publish, fixture) for fixture in (a, other)]
        results = [item.result(timeout=20) for item in pending]
    assert sum(item.returncode == 0 for item in results) == 1
    assert any('storage_quota_exceeded' in item.stderr for item in results)
    assert value(a) == value(other) == 6 and events(a) == 1
    assert a.authority.count('version_ref_transactions') + other.authority.count('version_ref_transactions') == 1
