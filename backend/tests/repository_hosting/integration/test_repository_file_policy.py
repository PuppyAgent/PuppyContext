"""Checked policy atomicity/admission; owner-installed facts are not S3 proof."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import A, oid, update
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_publication_pins import rpc
from tests.repository_hosting.integration.test_repository_capacity import item, usage
from tests.repository_hosting.integration.test_repository_logical_billing import (
    enroll_billing,
    events,
    query,
    value,
)
from tests.repository_hosting.integration.test_repository_write_admission import (
    admission as admission_fixture,
)
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential

pytestmark = pytest.mark.hosting_live
admission = admission_fixture


def enroll_file_policy(a, maximum=8):
    a.pg.sql(f"""
        INSERT INTO public.version_repository_file_policies(project_id,initialized) VALUES({literal(a.project)},true);
        UPDATE public.organization_entitlements SET entitlements=jsonb_set(entitlements,
            '{{limits,upload.max_single_file_bytes}}',{literal(json.dumps(maximum))}::jsonb) WHERE org_id={literal(a.org)};
    """)
    return a


@pytest.fixture
def policy(admission):
    a = enroll_billing(admission)
    a.pg.sql(f"""
        INSERT INTO public.version_organization_capacity(org_id,initialized) VALUES({literal(a.org)},true);
        INSERT INTO public.version_repository_capacity(project_id,org_id,initialized) VALUES({literal(a.project)},{literal(a.org)},true);
        INSERT INTO public.version_object_pins(id,project_id,actor,object_format,generation,gc_epoch,roots,state,expires_at)
            SELECT id,project_id,{literal(a.actor)},object_format,generation,gc_epoch,roots,'verified',expires_at
            FROM public.version_publication_receipts WHERE id={literal(a.authority.receipt)};
        INSERT INTO public.version_repository_capacity_proofs(pin_id,project_id,manifest_sha256)
            SELECT id,project_id,manifest_sha256 FROM public.version_publication_receipts WHERE id={literal(a.authority.receipt)};
    """)
    return enroll_file_policy(a)


def policy_query(a, key=None, proof=True, **changes):
    args = a.authority.parameters([update(new=oid(A))], actor=a.actor, key=key)
    measured = dict(org_id=a.org, source_revision=1, old_head_oid=None, new_head_oid=A, old_bytes=0, new_bytes=4)
    facts = dict(org_id=a.org, source_revision=1, file_limit=8, old_head_oid=None, new_head_oid=A, manifest_sha256='f'*64)
    facts.update(changes)
    return 'SET ROLE service_role; SELECT public.apply_policy_version_ref_transaction('+','.join(map(literal, (
        *args.values(), a.lease, a.holder, measured if proof else None, facts if proof else None)))+')'


def test_repository_file_policy_fences_old_publisher_and_replays_as_read(policy):
    a = policy
    old = a.pg.sql('SET ROLE service_role;'+query(a), check=False)
    assert old.returncode and 'repository_file_policy_coordination_required' in old.stderr
    assert value(a) == events(a) == 0
    key = str(uuid.uuid4())
    first = json.loads(a.pg.value(policy_query(a, key)))
    assert first['status'] == 'committed' and value(a) == 4 and events(a) == 1
    a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)};"
             f"UPDATE public.project_write_leases SET expires_at=clock_timestamp() WHERE id={literal(a.lease)};"
             f"UPDATE public.version_repository_file_policies SET initialized=false WHERE project_id={literal(a.project)};"
             f"DELETE FROM public.organization_entitlements WHERE org_id={literal(a.org)}")
    assert json.loads(a.pg.value(policy_query(a, key, proof=False))) == first
    assert value(a) == 4 and events(a) == 1


@pytest.mark.parametrize('change,expected', [
    ({'manifest_sha256': 'a'*64}, 'file_policy_manifest_mismatch'),
    ({'source_revision': 2}, 'storage_billing_entitlement_changed'),
    ({'file_limit': None}, 'file_policy_projection_changed'),
    ({'old_head_oid': A}, 'file_policy_snapshot_changed'),
    ({'new_head_oid': None}, 'file_policy_snapshot_changed'),
])
def test_repository_file_policy_rolls_back_all_effects(policy, change, expected):
    a = policy
    result = a.pg.sql(policy_query(a, **change), check=False)
    assert result.returncode and expected in result.stderr
    assert value(a) == events(a) == a.authority.count('version_ref_transactions') == 0
    assert a.authority.state() is None


def admitted_pin(a):
    pin = str(uuid.uuid4())
    rpc(a.pg, 'begin_admitted_version_object_publication', a.project, a.actor, pin, 1, {A: 'commit'}, a.lease, a.holder)
    return pin


@pytest.mark.parametrize('existing', [False, True])
def test_repository_file_policy_rechecks_actor_before_every_storage_claim(policy, existing):
    a = policy
    pin = admitted_pin(a)
    oversized = rpc(a.pg, 'reserve_version_object_capacity', a.project, a.actor, pin,
                    [item('1'*40, 9, 'blob')], check=False)
    assert oversized.returncode and 'file_size_limit_exceeded' in oversized.stderr
    assert usage(a.authority) == [0, 0]
    io = str(uuid.uuid4())
    rpc(a.pg, 'reserve_version_object_capacity', a.project, a.actor, pin, [item('1'*40, 4, 'blob')], True, io)
    assert usage(a.authority) == [4, 1]
    a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}")
    result = rpc(a.pg, 'reserve_version_object_capacity', a.project, a.actor, pin,
                 [item(('1' if existing else '2')*40, 4, 'blob')], check=False)
    assert result.returncode and 'repository_action_denied' in result.stderr
    assert usage(a.authority) == [4, 1]
    # Known-completed SQL-only work may settle after revocation; this confers no
    # fresh write authority and cannot settle another invocation.
    settled = rpc(a.pg, 'settle_version_object_capacity_io', a.project, a.actor, pin, io)
    assert json.loads(settled.stdout)['settled_objects'] == 1
    rpc(a.pg, 'begin_admitted_version_repository_read', a.project, a.actor, str(uuid.uuid4()))
    a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
    denied = rpc(a.pg, 'begin_admitted_version_repository_read', a.project, a.actor, str(uuid.uuid4()), check=False)
    assert denied.returncode and 'repository_action_denied' in denied.stderr


def test_repository_file_policy_uploading_pin_cannot_borrow_retry_lease(policy):
    a = policy
    pin = admitted_pin(a)
    a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp() WHERE id={literal(a.lease)}")
    lease = str(uuid.uuid4())
    rpc(a.pg, 'acquire_project_write_lease', a.project, lease, 'retry-holder', 'retry', 120)
    result = rpc(a.pg, 'begin_admitted_version_object_publication', a.project, a.actor, pin, 1,
                 {A: 'commit'}, lease, 'retry-holder', check=False)
    assert result.returncode and 'native_publication_admission_mismatch' in result.stderr
    assert a.pg.value(f"SELECT lease_id FROM public.version_publication_admissions WHERE pin_id={literal(pin)}") == a.lease
    # Owner simulation of completed/sealed SQL-only work. Real S3 tests must
    # prove physical completion, not infer it from these fixture rows.
    a.pg.sql(f"UPDATE public.version_object_pins SET state='verified' WHERE id={literal(pin)}")
    rpc(a.pg, 'begin_admitted_version_object_publication', a.project, a.actor, pin, 1, {A: 'commit'}, lease, 'retry-holder')
    late = rpc(a.pg, 'reserve_version_object_capacity', a.project, a.actor, pin, [item('1'*40)], check=False)
    assert late.returncode and 'publication_pin_unavailable' in late.stderr


def queued_rpc(pg, name, application_name, *args):
    return pg.sql(f'SET application_name={literal(application_name)}; SET ROLE service_role; SELECT public.{name}('
                  + ','.join(map(literal, args)) + ')', check=False)


@pytest.mark.parametrize('expires', ['lease', 'entitlement'])
def test_repository_file_policy_expiry_after_entitlement_wait_rolls_back_claims(policy, expires):
    a = policy
    pin = admitted_pin(a)
    if expires == 'lease':
        a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(a.lease)}")
    else:
        a.pg.sql(f"UPDATE public.organization_entitlements SET effective_until=clock_timestamp()+interval '2 seconds' WHERE org_id={literal(a.org)}")
    name = 'file-policy-'+uuid.uuid4().hex
    with ThreadPoolExecutor() as pool:
        with transaction(a.pg, f"SELECT 1 FROM public.organization_entitlements WHERE org_id={literal(a.org)} FOR UPDATE"):
            pending = pool.submit(queued_rpc, a.pg, 'reserve_version_object_capacity', name, a.project, a.actor, pin,
                                  [item('1'*40, 4, 'blob')])
            wait_for_lock(a.pg, name)
            time.sleep(2.1)
        result = pending.result(timeout=10)
    expected = 'repository_write_lease_unavailable' if expires == 'lease' else 'storage_billing_entitlement_invalid'
    assert result.returncode and expected in result.stderr
    assert usage(a.authority) == [0, 0]
    assert a.pg.value(f"SELECT count(*) FROM public.version_repository_capacity_inflight WHERE project_id={literal(a.project)}") == '0'


@pytest.mark.parametrize('metadata_only', [False, True])
def test_repository_file_policy_read_admission_rechecks_expiry_after_repository_wait(policy, metadata_only):
    a = policy
    _, credential, actor = seed_credential(a)
    a.pg.sql(f"UPDATE public.access_surface_credentials SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(credential)}")
    pin = str(uuid.uuid4())
    name = 'file-read-'+uuid.uuid4().hex
    with ThreadPoolExecutor() as pool:
        with transaction(a.pg, f"SELECT 1 FROM public.version_repositories WHERE project_id={literal(a.project)} FOR UPDATE"):
            function = 'get_admitted_version_repository_snapshot' if metadata_only else 'begin_admitted_version_repository_read'
            args = (a.project, actor) if metadata_only else (a.project, actor, pin)
            pending = pool.submit(queued_rpc, a.pg, function, name, *args)
            wait_for_lock(a.pg, name)
            time.sleep(2.1)
        result = pending.result(timeout=10)
    assert result.returncode and 'repository_action_denied' in result.stderr
    assert a.pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE id={literal(pin)}") == '0'


def test_repository_file_policy_acl(policy):
    public = ['check_version_repository_file_policy(text)',
              'begin_admitted_version_object_publication(text,text,uuid,bigint,jsonb,uuid,text)',
              'begin_admitted_version_repository_read(text,text,uuid)',
              'get_admitted_version_repository_snapshot(text,text)',
              'apply_policy_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text,jsonb,jsonb)']
    private = ['_version_file_policy_limit(text,bigint)', '_version_assert_native_pin_admission(text,uuid)',
               '_version_fence_native_io_admission()', '_version_fence_new_blob_size()', '_version_fence_file_policy_publication()']
    for name in public + private:
        for role in ('anon', 'authenticated', 'service_role'):
            allowed = policy.pg.value(f"SELECT has_function_privilege({literal(role)},{literal('public.'+name)},'EXECUTE')")
            assert allowed == ('t' if role == 'service_role' and name in public else 'f')
        assert policy.pg.value(f"SELECT proconfig::text FROM pg_proc WHERE oid={literal('public.'+name)}::regprocedure") == '{"search_path=pg_catalog, public, pg_temp"}'
