"""Attempt metadata/fencing with real PG; not physical I/O quiescence proof."""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.version_engine.write_engine.ref_transaction import publication_pin_id
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import update
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_product_operation_journal import (
    DIGEST,
    begin_query,
    call,
    prepare_query,
)
from tests.repository_hosting.integration.test_product_operation_journal import (
    journal_actor as journal_actor_fixture,
)

pytestmark = pytest.mark.hosting_live
journal_actor = journal_actor_fixture


@pytest.fixture
def prepared_attempt(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    oid = 'b' * (40 if a.fmt == 'sha1' else 64)
    plan = {'updates': [update(b'refs/heads/topic', {'kind': 'absent'}, {'kind': 'oid', 'oid': oid})],
            'receipt_id': publication_pin_id(a.project, a.actor, key), 'message': 'attempt fixture',
            'product_result': {'tree_oid': 'c'*len(oid), 'commit_oid': oid, 'changes': []}}
    call(a, begin_query(a, key))
    record = call(a, prepare_query(a, key, plan))
    return a, key, record


def open_query(a, key, attempt, digest=DIGEST):
    return 'SELECT public.open_admitted_version_product_attempt(' + ','.join(map(literal, (
        a.project, a.actor, key, digest, 1, attempt, a.lease, a.holder,
    ))) + ')'


def inventory(a):
    return json.loads(a.pg.value(f"SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY pin_id),'[]') "
        f"FROM public.version_product_publication_attempts a WHERE project_id={literal(a.project)}"))


def retry_worker(a):
    other = SimpleNamespace(**vars(a))
    other.lease, other.holder = str(uuid.uuid4()), 'retry-worker'
    a.pg.sql('SELECT public.acquire_project_write_lease(' + ','.join(map(literal, (
        a.project, other.lease, other.holder, 'native-product-retry', 120,
    ))) + ')')
    return other


def test_attempt_identity_keeps_original_candidate_and_clock(prepared_attempt):
    a, key, original = prepared_attempt
    attempt = str(uuid.uuid4())
    opened = call(a, open_query(a, key, attempt))
    assert opened['created_at'] == original['created_at']
    assert opened['input_sha256'] == original['input_sha256']
    assert opened['proposal'] == {**original['proposal'], 'receipt_id': attempt}
    assert opened['native_request_sha256'] != original['native_request_sha256']
    assert call(a, open_query(a, key, attempt)) == opened
    # Original preparation is immutable; attempt metadata is not a pin or a ref.
    assert call(a, begin_query(a, key)) == original
    assert a.pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE project_id={literal(a.project)}") == '0'
    assert sum(row['active'] for row in inventory(a)) == 1
    before = inventory(a)
    denied = a.pg.sql('SET ROLE service_role;' + open_query(a, key, str(uuid.uuid4()), '2'*64), check=False)
    assert denied.returncode and 'request_key_reused' in denied.stderr
    assert inventory(a) == before


def test_live_other_invocation_is_busy_but_expired_lease_does_not_settle_io(prepared_attempt):
    a, key, original = prepared_attempt
    first = str(uuid.uuid4())
    call(a, open_query(a, key, first))
    other = retry_worker(a)
    before = inventory(a)
    denied = a.pg.sql('SET ROLE service_role;' + open_query(other, key, str(uuid.uuid4())), check=False)
    assert denied.returncode and 'product_operation_busy' in denied.stderr
    assert inventory(a) == before
    oid = original['proposal']['product_result']['commit_oid']
    roots = {oid: 'commit'}
    call(a, 'SELECT public.begin_admitted_version_object_publication(' + ','.join(map(literal, (
        a.project, a.actor, first, 1, roots, a.lease, a.holder,
    ))) + ')')
    a.pg.sql(f"INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects) "
             f"VALUES({literal(a.org)},true,100,10); "
             f"INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects) "
             f"VALUES({literal(a.project)},{literal(a.org)},true,100,10)")
    call(a, 'SELECT public.reserve_version_object_capacity(' + ','.join(map(literal, (
        a.project, a.actor, first, [{'object_id': oid, 'body_bytes': 6, 'object_kind': 'commit'}],
    ))) + ')')
    claims_sql = (f"SELECT jsonb_agg(to_jsonb(i) ORDER BY io_id) FROM public.version_repository_capacity_inflight i "
                  f"WHERE project_id={literal(a.project)}")
    claims = a.pg.value(claims_sql)
    assert json.loads(claims)
    a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(a.lease)}")
    second = str(uuid.uuid4())
    opened = call(a, open_query(other, key, second))
    assert opened['proposal'] == {**original['proposal'], 'receipt_id': second}
    assert a.pg.value(claims_sql) == claims  # Not settlement, even for this SQL-only fixture.
    assert a.pg.value(f"SELECT state FROM public.version_object_pins WHERE id={literal(first)}") == 'released'
    assert [row['pin_id'] for row in inventory(a) if row['active']] == [second]


def test_retired_never_started_pin_cannot_be_created_by_old_worker(prepared_attempt):
    a, key, original = prepared_attempt
    call(a, open_query(a, key, str(uuid.uuid4())))
    old_pin = original['proposal']['receipt_id']
    roots = {original['proposal']['product_result']['commit_oid']: 'commit'}
    query = 'SELECT public.begin_admitted_version_object_publication(' + ','.join(map(literal, (
        a.project, a.actor, old_pin, 1, roots, a.lease, a.holder,
    ))) + ')'
    denied = a.pg.sql('SET ROLE service_role;' + query, check=False)
    assert denied.returncode and 'product_attempt_superseded' in denied.stderr
    assert a.pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE id={literal(old_pin)}") == '0'


def test_upload_attempt_cannot_borrow_a_different_invocations_lease(prepared_attempt):
    a, key, original = prepared_attempt
    pin = str(uuid.uuid4())
    call(a, open_query(a, key, pin))
    other = retry_worker(a)
    roots = {original['proposal']['product_result']['commit_oid']: 'commit'}
    query = 'SELECT public.begin_admitted_version_object_publication(' + ','.join(map(literal, (
        a.project, a.actor, pin, 1, roots, other.lease, other.holder,
    ))) + ')'
    denied = a.pg.sql('SET ROLE service_role;' + query, check=False)
    assert denied.returncode and 'product_attempt_admission_mismatch' in denied.stderr
    assert a.pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE id={literal(pin)}") == '0'


def test_only_active_attempt_can_record_result_then_replay_is_read_only(prepared_attempt):
    a, key, original = prepared_attempt
    old = call(a, open_query(a, key, str(uuid.uuid4())))
    current = call(a, open_query(a, key, str(uuid.uuid4())))
    result = {'project_id': a.project, 'generation': 1, 'status': 'rejected', 'reason': 'SQL fixture'}
    # Owner-only injection tests the publication trigger, not physical publication.
    def record_query(digest):
        return ("INSERT INTO public.version_ref_transactions(id,project_id,actor,request_key,request_sha256,result) VALUES("
                + ','.join(map(literal, (str(uuid.uuid4()), a.project, a.actor, key, digest, result))) + ')')
    denied = a.pg.sql(record_query(old['native_request_sha256']), check=False)
    assert denied.returncode and 'request_key_reused' in denied.stderr
    a.pg.sql(record_query(current['native_request_sha256']))
    a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE project_id={literal(a.project)} AND user_id={literal(a.user)}; "
             f"DELETE FROM public.project_write_leases WHERE id={literal(a.lease)}")
    before = inventory(a)
    replay = call(a, open_query(a, key, str(uuid.uuid4())))
    assert replay['result'] == result
    assert replay['proposal'] == original['proposal']
    assert inventory(a) == before
    assert call(a, begin_query(a, key))['result'] == result
    a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
    denied = a.pg.sql('SET ROLE service_role;' + open_query(a, key, str(uuid.uuid4())), check=False)
    assert denied.returncode and 'repository_action_denied' in denied.stderr
    assert inventory(a) == before


def test_attempt_inventory_and_helpers_are_not_client_or_direct_mutation_authority(journal_actor):
    a = journal_actor
    rpc = 'public.open_admitted_version_product_attempt(text,text,uuid,text,bigint,uuid,uuid,text)'
    for role in ('anon', 'authenticated', 'service_role'):
        assert a.pg.value(f"SELECT has_function_privilege({literal(role)},{literal(rpc)},'EXECUTE')") == ('t' if role == 'service_role' else 'f')
        for helper in ('_version_fence_product_attempt_pin()', '_version_fence_product_attempt_admission()'):
            assert a.pg.value(f"SELECT has_function_privilege({literal(role)},{literal('public.'+helper)},'EXECUTE')") == 'f'
        for privilege in ('INSERT', 'UPDATE', 'DELETE'):
            assert a.pg.value(f"SELECT has_table_privilege({literal(role)},'public.version_product_publication_attempts',{literal(privilege)})") == 'f'
    assert a.pg.value("SELECT relrowsecurity FROM pg_class WHERE oid='public.version_product_publication_attempts'::regclass") == 't'


def test_attempt_retirement_rolls_back_when_lease_expires_behind_pin_lock(prepared_attempt):
    a, key, original = prepared_attempt
    pin = str(uuid.uuid4())
    call(a, open_query(a, key, pin))
    roots = {original['proposal']['product_result']['commit_oid']: 'commit'}
    call(a, 'SELECT public.begin_admitted_version_object_publication(' + ','.join(map(literal, (
        a.project, a.actor, pin, 1, roots, a.lease, a.holder,
    ))) + ')')
    before = inventory(a)
    a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()+interval '500 milliseconds' WHERE id={literal(a.lease)}")
    name = 'product-attempt-pin-wait-' + uuid.uuid4().hex
    query = f"SET application_name={literal(name)}; SET ROLE service_role;" + open_query(a, key, str(uuid.uuid4()))
    with ThreadPoolExecutor(1) as pool:
        with transaction(a.pg, f"SELECT id FROM public.version_object_pins WHERE id={literal(pin)} FOR UPDATE"):
            pending = pool.submit(a.pg.sql, query, check=False)
            wait_for_lock(a.pg, name)
            time.sleep(0.6)
        denied = pending.result(timeout=10)
    assert denied.returncode and 'repository_write_lease_unavailable' in denied.stderr
    assert inventory(a) == before
    assert a.pg.value(f"SELECT state FROM public.version_object_pins WHERE id={literal(pin)}") == 'uploading'


def test_attempt_admission_rechecks_lease_after_lock_wait(prepared_attempt):
    a, key, _ = prepared_attempt
    a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()+interval '500 milliseconds' WHERE id={literal(a.lease)}")
    name = 'product-attempt-expiry-' + uuid.uuid4().hex
    query = f"SET application_name={literal(name)}; SET ROLE service_role;" + open_query(a, key, str(uuid.uuid4()))
    with ThreadPoolExecutor(1) as pool:
        with transaction(a.pg, f"SELECT id FROM public.project_write_leases WHERE id={literal(a.lease)} FOR UPDATE"):
            pending = pool.submit(a.pg.sql, query, check=False)
            wait_for_lock(a.pg, name)
            time.sleep(0.6)
        denied = pending.result(timeout=10)
    assert denied.returncode and 'repository_write_lease_unavailable' in denied.stderr
    assert inventory(a) == []
