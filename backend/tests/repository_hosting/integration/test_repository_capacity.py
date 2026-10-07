"""Technical capacity SQL/locking/ledger, not remote I/O or customer billing proof."""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import Authority, update
from tests.repository_hosting.harness.ref_authority import oid as oid_state
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_publication_pins import begin, gc, rpc

pytestmark = pytest.mark.hosting_live


def setup(pg_project, object_format='sha1'):
    pg, project = pg_project
    oid = 'a' * (40 if object_format == 'sha1' else 64)
    authority = Authority(pg, project, object_format=object_format, roots={oid: 'commit'})
    org = pg.value(f"SELECT org_id FROM public.projects WHERE id={literal(project)}")
    pg.sql(f"INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects) "
           f"VALUES({literal(org)},true,10,2) ON CONFLICT DO NOTHING")
    pg.sql(f"INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects) "
           f"VALUES({literal(project)},{literal(org)},true,10,2)")
    pin, _ = begin(authority, roots={oid: 'commit'})
    return authority, pin, oid


def item(oid, size=6, kind='commit'):
    return dict(object_id=oid, body_bytes=size, object_kind=kind)


def reserve(authority, pin, objects, *, actor='test:writer', check=True):
    return rpc(authority.pg, 'reserve_version_object_capacity', authority.project, actor, pin, objects, check=check)


def settle_sql_only_producer(authority, pin):
    # These fixtures only issue SQL: there is provably no physical or index I/O.
    ids = json.loads(authority.pg.value("SELECT COALESCE(jsonb_agg(DISTINCT io_id),'[]') "
        f"FROM public.version_repository_capacity_inflight WHERE project_id={literal(authority.project)} AND pin_id={literal(pin)}"))
    for io_id in ids:
        rpc(authority.pg, 'settle_version_object_capacity_io', authority.project, 'test:writer', pin, io_id)


def usage(authority):
    return json.loads(authority.pg.value(
        f"SELECT jsonb_build_array(used_body_bytes,used_objects) FROM public.version_repository_capacity "
        f"WHERE project_id={literal(authority.project)}"))


def org_usage(authority):
    return json.loads(authority.pg.value(
        f"SELECT jsonb_build_array(o.used_body_bytes,o.used_objects) FROM public.version_organization_capacity o "
        f"JOIN public.version_repository_capacity p ON p.org_id=o.org_id WHERE p.project_id={literal(authority.project)}"))


def events(authority):
    return json.loads(authority.pg.value(
        "SELECT COALESCE(jsonb_agg(jsonb_build_array(action,body_bytes_delta,objects_delta) ORDER BY id),'[]') "
        f"FROM public.version_repository_capacity_events WHERE project_id={literal(authority.project)}"))


def collect(authority, token, oids, *, check=True):
    return authority.pg.sql("SET ROLE service_role; SELECT public.collect_version_object_capacity("
                            f"{literal(authority.project)},{literal(token)},ARRAY["
                            + ','.join(literal(oid) for oid in oids) + ']::text[]);', check=check)


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_capacity_is_exact_idempotent_and_atomic_for_whole_batch(pg_project, object_format):
    auth, pin, oid = setup(pg_project, object_format)
    first = item(oid)
    assert json.loads(reserve(auth, pin, [first]).stdout) == {'new_body_bytes': 6, 'new_objects': 1}
    assert json.loads(reserve(auth, pin, [first]).stdout) == {'new_body_bytes': 0, 'new_objects': 0}
    assert usage(auth) == [6, 1]
    # The first new object fits, but the complete batch does not. Neither lands.
    denied = reserve(auth, pin, [item('b' * len(oid), 1), item('c' * len(oid), 5)], check=False)
    assert denied.returncode and 'repository_capacity_exceeded' in denied.stderr
    assert usage(auth) == [6, 1]
    assert events(auth) == [['reserve', 6, 1]]
    changed = reserve(auth, pin, [item(oid, 5)], check=False)
    assert changed.returncode and 'repository_object_identity_mismatch' in changed.stderr
    assert usage(auth) == [6, 1]


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_capacity_concurrent_distinct_proposals_cannot_overspend(pg_project, object_format):
    auth, first_pin, oid = setup(pg_project, object_format)
    second_pin, _ = begin(auth, roots={oid: 'commit'})
    gate = Barrier(2)

    def allocate(pin, object_id):
        gate.wait(timeout=10)
        return reserve(auth, pin, [item(object_id)], check=False)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(allocate, pin, name * len(oid))
                   for pin, name in ((first_pin, 'b'), (second_pin, 'c'))]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result.returncode == 0 for result in results) == 1
    assert any('repository_capacity_exceeded' in result.stderr for result in results)
    assert usage(auth) == [6, 1]
    assert events(auth) == [['reserve', 6, 1]]


def test_capacity_is_atomic_across_two_projects_in_one_organization(pg_project):
    first, first_pin, _ = setup(pg_project)
    second_id = str(uuid.uuid4())
    first.pg.sql(f"BEGIN; INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status) "
                 f"SELECT {literal(second_id)},'capacity-sibling',org_id,created_by,'ready' FROM public.projects "
                 f"WHERE id={literal(first.project)}; "
                 f"INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by) "
                 f"SELECT {literal('pm-' + second_id)},org_id,id,created_by,'admin',created_by FROM public.projects "
                 f"WHERE id={literal(second_id)}; COMMIT;")
    second, second_pin, _ = setup((first.pg, second_id))
    gate = Barrier(2)

    def allocate(authority, pin):
        gate.wait(timeout=10)
        return reserve(authority, pin, [item('c' * 40)], check=False)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(allocate, auth, pin) for auth, pin in ((first, first_pin), (second, second_pin))]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result.returncode == 0 for result in results) == 1
    assert any('repository_capacity_exceeded' in result.stderr for result in results)
    assert sorted([usage(first), usage(second)]) == [[0, 0], [6, 1]]
    assert org_usage(first) == org_usage(second) == [6, 1]


@pytest.mark.parametrize('state', ['expired', 'released', 'generation', 'epoch', 'actor', 'pin', 'uninitialized', 'missing-policy', 'org-uninitialized'])
def test_capacity_requires_live_matching_publication_and_explicit_inventory(pg_project, state):
    auth, pin, oid = setup(pg_project)
    actor = 'test:writer'
    if state == 'expired':
        auth.pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(pin)}")
    elif state == 'released':
        rpc(auth.pg, 'release_version_object_publication', auth.project, actor, pin)
    elif state in {'generation', 'epoch'}:
        column = 'generation' if state == 'generation' else 'gc_epoch'
        auth.pg.sql(f"UPDATE public.version_repositories SET {column}={column}+1 WHERE project_id={literal(auth.project)}")
    elif state == 'actor':
        actor = 'test:other'
    elif state == 'pin':
        pin = str(uuid.uuid4())
    elif state == 'org-uninitialized':
        auth.pg.sql(f"UPDATE public.version_organization_capacity SET initialized=false WHERE org_id IN "
                     f"(SELECT org_id FROM public.projects WHERE id={literal(auth.project)})")
    elif state == 'uninitialized':
        auth.pg.sql(f"UPDATE public.version_repository_capacity SET initialized=false WHERE project_id={literal(auth.project)}")
    else:
        auth.pg.sql(f"DELETE FROM public.version_repository_capacity WHERE project_id={literal(auth.project)}")
    result = reserve(auth, pin, [item(oid)], actor=actor, check=False)
    assert result.returncode
    assert ('publication_pin_unavailable' in result.stderr or 'repository_capacity_uninitialized' in result.stderr)
    assert events(auth) == []
    if state != 'missing-policy':
        assert usage(auth) == [0, 0]


@pytest.mark.parametrize('objects', [[], {}, [item('a' * 40, -1)], [item('a' * 40, 1.5)],
                                      [item('a' * 40, 1, 'gitlink')], [item('a' * 64)],
                                      [item('a' * 40), item('a' * 40)],
                                      [item(f'{i + 1:040x}', 0) for i in range(201)]])
def test_invalid_capacity_batches_do_not_allocate(pg_project, objects):
    auth, pin, _ = setup(pg_project)
    result = reserve(auth, pin, objects, check=False)
    assert result.returncode and 'invalid_capacity_objects' in result.stderr
    assert usage(auth) == [0, 0]
    assert events(auth) == []


def test_expiry_does_not_refund_and_only_current_gc_can_release_once(pg_project):
    auth, pin, oid = setup(pg_project)
    reserve(auth, pin, [item(oid)])
    auth.pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(pin)}")
    assert usage(auth) == [6, 1]
    token = str(uuid.uuid4())
    gc(auth, token)
    denied = collect(auth, str(uuid.uuid4()), [oid], check=False)
    assert denied.returncode and 'repository_gc_token_mismatch' in denied.stderr
    assert usage(auth) == [6, 1]
    unsettled = collect(auth, token, [oid], check=False)
    assert unsettled.returncode and 'repository_capacity_io_unsettled' in unsettled.stderr
    # This SQL-only producer issued no physical I/O. Explicitly settle it; expiry
    # alone above was not permission to refund a possibly outstanding upload.
    rpc(auth.pg, 'release_version_object_publication', auth.project, 'test:writer', pin)
    settle_sql_only_producer(auth, pin)
    assert json.loads(collect(auth, token, [oid]).stdout) == {'removed_body_bytes': 6, 'removed_objects': 1}
    assert json.loads(collect(auth, token, [oid]).stdout) == {'removed_body_bytes': 0, 'removed_objects': 0}
    assert usage(auth) == [0, 0]
    assert events(auth) == [['reserve', 6, 1], ['collect', -6, -1]]
    assert org_usage(auth) == [0, 0]
    rpc(auth.pg, 'finish_version_repository_gc', auth.project, token)
    pin, _ = begin(auth)
    reserve(auth, pin, [item(oid)])
    assert collect(auth, token, [oid], check=False).returncode
    assert usage(auth) == [6, 1]


@pytest.mark.parametrize('barrier', ['policy', 'object'])
def test_queued_expiry_rolls_back_capacity_counters_claims_and_ledger(pg_project, barrier):
    auth, pin, oid = setup(pg_project)
    objects = [item(oid)]
    if barrier == 'object':
        reserve(auth, pin, objects)
        objects.append(item('b' * 40, 4))
        lock = f"SELECT 1 FROM public.version_repository_object_capacity WHERE project_id={literal(auth.project)} FOR UPDATE"
    else:
        lock = f"SELECT 1 FROM public.version_organization_capacity WHERE org_id IN (SELECT org_id FROM public.projects WHERE id={literal(auth.project)}) FOR UPDATE"
    before, old_events = usage(auth), events(auth)
    name = 'capacity-expiry-' + uuid.uuid4().hex
    auth.pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(pin)}")
    query = (f"SET application_name={literal(name)}; SET ROLE service_role; SELECT public.reserve_version_object_capacity("
             f"{literal(auth.project)},'test:writer',{literal(pin)},{literal(objects)})")
    with ThreadPoolExecutor(1) as pool:
        with transaction(auth.pg, lock) as session:
            pending = pool.submit(auth.pg.sql, query, check=False)
            wait_for_lock(auth.pg, name)
            deadline = time.monotonic() + 5
            while auth.pg.value(f"SELECT expires_at<=clock_timestamp() FROM public.version_object_pins WHERE id={literal(pin)}") != 't':
                assert time.monotonic() < deadline, 'capacity expiry barrier timed out'
                time.sleep(.02)
            session.execute('COMMIT')
        result = pending.result(timeout=10)
    assert result.returncode and 'publication_pin_unavailable' in result.stderr
    assert usage(auth) == org_usage(auth) == before and events(auth) == old_events
    assert auth.pg.value(f"SELECT count(*) FROM public.version_repository_capacity_inflight "
                         f"WHERE project_id={literal(auth.project)}") == ('1' if barrier == 'object' else '0')


def test_capacity_inventory_is_bounded_ordered_and_token_bound(pg_project):
    auth, pin, _oid = setup(pg_project)
    auth.pg.sql(f"UPDATE public.version_repository_capacity SET max_objects=1000 WHERE project_id={literal(auth.project)}; "
                 f"UPDATE public.version_organization_capacity SET max_objects=1000 WHERE org_id IN "
                 f"(SELECT org_id FROM public.projects WHERE id={literal(auth.project)})")
    objects = [item(f'{i + 1:040x}', 0, 'blob') for i in range(201)]
    reserve(auth, pin, objects[:200])
    reserve(auth, pin, objects[200:])
    denied = rpc(auth.pg, 'get_version_repository_capacity_inventory', auth.project, pin, '', 200, check=False)
    assert denied.returncode and 'repository_gc_token_mismatch' in denied.stderr
    rpc(auth.pg, 'release_version_object_publication', auth.project, 'test:writer', pin)
    settle_sql_only_producer(auth, pin)
    token = str(uuid.uuid4())
    gc(auth, token)
    first = json.loads(rpc(auth.pg, 'get_version_repository_capacity_inventory', auth.project, token, '', 200).stdout)['objects']
    assert len(first) == 200 and all(not row['unsettled'] for row in first.values())
    second = json.loads(rpc(auth.pg, 'get_version_repository_capacity_inventory', auth.project, token, max(first), 200).stdout)['objects']
    assert list(second) == [objects[-1]['object_id']]
    assert rpc(auth.pg, 'get_version_repository_capacity_inventory', auth.project, token, '', 201, check=False).returncode
    rpc(auth.pg, 'finish_version_repository_gc', auth.project, token)
    assert rpc(auth.pg, 'get_version_repository_capacity_inventory', auth.project, token, '', 200, check=False).returncode


def test_capacity_rpc_and_private_trigger_privileges(pg_project):
    pg, _project = pg_project
    exposed = ['check_version_repository_capacity(text)', 'reserve_version_object_capacity(text,text,uuid,jsonb,boolean,uuid)',
               'seal_capacity_version_object_publication(text,text,uuid,text,jsonb)',
               'collect_version_object_capacity(text,uuid,text[])',
               'get_version_repository_capacity_inventory(text,uuid,text,integer)',
               'settle_version_object_capacity_io(text,text,uuid,uuid)']
    private = ['_version_fence_capacity_publication()']
    for signature in exposed + private:
        for role in ('anon', 'authenticated', 'service_role'):
            actual = pg.value(f"SELECT has_function_privilege({literal(role)},{literal('public.' + signature)},'EXECUTE')")
            assert actual == ('t' if role == 'service_role' and signature in exposed else 'f')
        assert 'pg_catalog, public, pg_temp' in pg.value(
            f"SELECT proconfig::text FROM pg_proc WHERE oid={literal('public.' + signature)}::regprocedure")


def test_deduplicated_reupload_still_holds_capacity_until_io_settles(pg_project):
    auth, pin, oid = setup(pg_project)
    reserve(auth, pin, [item(oid)])
    rpc(auth.pg, 'release_version_object_publication', auth.project, 'test:writer', pin)
    settle_sql_only_producer(auth, pin)
    pin, _ = begin(auth)
    assert json.loads(reserve(auth, pin, [item(oid)]).stdout)['new_objects'] == 0
    auth.pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(pin)}")
    token = str(uuid.uuid4())
    gc(auth, token)
    denied = collect(auth, token, [oid], check=False)
    assert denied.returncode and 'repository_capacity_io_unsettled' in denied.stderr
    assert usage(auth) == org_usage(auth) == [6, 1]
    rpc(auth.pg, 'release_version_object_publication', auth.project, 'test:writer', pin)
    settle_sql_only_producer(auth, pin)
    collect(auth, token, [oid])
    assert usage(auth) == org_usage(auth) == [0, 0]


def test_context_flag_cannot_opt_out_of_an_enrolled_capacity_limit(pg_project):
    auth, pin, oid = setup(pg_project)
    result = rpc(auth.pg, 'reserve_version_object_capacity', auth.project, 'test:writer', pin,
                 [item(oid, 11)], False, check=False)
    assert result.returncode and 'repository_capacity_exceeded' in result.stderr
    assert usage(auth) == org_usage(auth) == [0, 0]


def test_old_seal_and_old_receipt_cannot_bypass_capacity_publication(pg_project):
    auth, pin, oid = setup(pg_project)
    raw = rpc(auth.pg, 'seal_version_object_publication', auth.project, 'test:writer', pin, 'f' * 64, check=False)
    assert raw.returncode and 'publication_capacity_proof_required' in raw.stderr
    result = auth.pg.sql(auth.query([update(new=oid_state(oid))]), check=False)
    assert result.returncode and 'publication_capacity_proof_required' in result.stderr
    assert auth.state() is None and auth.count('version_ref_transactions') == 0
    assert auth.pg.value(f"SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(auth.project)}") == '0'


@pytest.mark.parametrize('transition', ['seal', 'release'])
def test_pin_transition_cannot_settle_other_attempt_io(pg_project, transition):
    auth, pin, oid = setup(pg_project)
    reserve(auth, pin, [item(oid, kind='commit')])
    # A retry sharing the same operation/pin cannot know that the earlier
    # producer's physical I/O has completed, even if it can verify the closure.
    if transition == 'seal':
        result = rpc(auth.pg, 'seal_capacity_version_object_publication', auth.project, 'test:writer', pin,
                     'f' * 64, {oid: {'kind': 'commit', 'peeled_oid': None}}, check=False)
        assert result.returncode and 'repository_capacity_io_unsettled' in result.stderr
    else:
        rpc(auth.pg, 'release_version_object_publication', auth.project, 'test:writer', pin)
    assert auth.pg.value(f"SELECT count(*) FROM public.version_repository_capacity_inflight "
                         f"WHERE project_id={literal(auth.project)}") == '1'


def test_capacity_seal_is_digest_bound_and_invalid_seal_rolls_back_proof(pg_project):
    auth, pin, oid = setup(pg_project)
    reserve(auth, pin, [item(oid)])
    details = {oid: {'kind': 'commit', 'peeled_oid': None}}
    denied = rpc(auth.pg, 'seal_capacity_version_object_publication', auth.project, 'test:other', pin, 'f' * 64, details, check=False)
    assert denied.returncode and 'publication_pin_unavailable' in denied.stderr
    assert auth.pg.value(f"SELECT count(*) FROM public.version_repository_capacity_proofs WHERE pin_id={literal(pin)}") == '0'
    settle_sql_only_producer(auth, pin)
    rpc(auth.pg, 'seal_capacity_version_object_publication', auth.project, 'test:writer', pin, 'f' * 64, details)
    denied = rpc(auth.pg, 'seal_capacity_version_object_publication', auth.project, 'test:writer', pin, 'e' * 64, details, check=False)
    assert denied.returncode and 'publication_capacity_proof_mismatch' in denied.stderr
    auth.receipt = pin
    assert auth.apply([update(new=oid_state(oid))])['status'] == 'committed'
    assert usage(auth) == org_usage(auth) == [6, 1]


def test_same_pin_io_attempts_have_independent_idempotent_settlement(pg_project):
    auth, pin, oid = setup(pg_project)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    for io_id in (first, second):
        rpc(auth.pg, 'reserve_version_object_capacity', auth.project, 'test:writer', pin, [item(oid)], True, io_id)
    assert usage(auth) == [6, 1]
    for expected in (1, 0):
        result = rpc(auth.pg, 'settle_version_object_capacity_io', auth.project, 'test:writer', pin, second)
        assert json.loads(result.stdout) == {'settled_objects': expected}
    denied = rpc(auth.pg, 'settle_version_object_capacity_io', auth.project, 'test:other', pin, first, check=False)
    assert denied.returncode and 'publication_pin_unavailable' in denied.stderr
    details = {oid: {'kind': 'commit', 'peeled_oid': None}}
    denied = rpc(auth.pg, 'seal_capacity_version_object_publication', auth.project, 'test:writer', pin, 'f' * 64, details, check=False)
    assert denied.returncode and 'repository_capacity_io_unsettled' in denied.stderr
    assert auth.pg.value(f"SELECT count(*) FROM public.version_repository_capacity_proofs WHERE pin_id={literal(pin)}") == '0'
    rpc(auth.pg, 'settle_version_object_capacity_io', auth.project, 'test:writer', pin, first)
    rpc(auth.pg, 'seal_capacity_version_object_publication', auth.project, 'test:writer', pin, 'f' * 64, details)


def test_capacity_reduction_preserves_old_allocations_and_dedup_retry(pg_project):
    auth, pin, oid = setup(pg_project)
    reserve(auth, pin, [item(oid)])
    auth.pg.sql(f"UPDATE public.version_repository_capacity SET max_body_bytes=1,max_objects=0 WHERE project_id={literal(auth.project)}")
    assert json.loads(reserve(auth, pin, [item(oid)]).stdout)['new_objects'] == 0
    assert reserve(auth, pin, [item('b' * 40, 0)], check=False).returncode
    assert usage(auth) == [6, 1]


def test_project_deletion_does_not_erase_physical_cleanup_identity_or_refund_capacity(pg_project):
    auth, pin, oid = setup(pg_project)
    reserve(auth, pin, [item(oid)])
    auth.pg.sql(f"DELETE FROM public.projects WHERE id={literal(auth.project)}")
    assert usage(auth) == org_usage(auth) == [6, 1]
    assert events(auth) == [['reserve', 6, 1]]
    assert auth.pg.value(f"SELECT object_id FROM public.version_repository_object_capacity "
                         f"WHERE project_id={literal(auth.project)}") == oid


def test_clients_and_service_dml_cannot_forge_capacity_or_ledger(pg_project):
    auth, pin, oid = setup(pg_project)
    for role in ('anon', 'authenticated', 'service_role'):
        result = auth.pg.sql(f"SET ROLE {role}; UPDATE public.version_repository_capacity SET used_body_bytes=0 "
                             f"WHERE project_id={literal(auth.project)}", check=False)
        assert result.returncode and 'permission denied' in result.stderr
    for role in ('anon', 'authenticated'):
        result = auth.pg.sql(f"SET ROLE {role}; SELECT public.reserve_version_object_capacity("
                             f"{literal(auth.project)},'test:writer',{literal(pin)},{literal([item(oid)])})", check=False)
        assert result.returncode and 'permission denied' in result.stderr
    assert usage(auth) == [0, 0]
