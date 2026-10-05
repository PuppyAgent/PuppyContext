"""Checked inventory/settlement on PG; synthetic measurements are not S3 proof."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_repository_logical_billing import (
    admission as admission_fixture,
)
from tests.repository_hosting.integration.test_repository_logical_billing import (
    billing as billing_fixture,
)
from tests.repository_hosting.integration.test_repository_logical_billing import (
    events,
    query,
    value,
)

pytestmark = pytest.mark.hosting_live
admission, billing = admission_fixture, billing_fixture


def call(a, function, *args, check=True):
    sql = 'SET ROLE service_role; SELECT to_jsonb(public.'+function+'('+','.join(map(literal, (a.org, *args)))+'))'
    if not check:
        return a.pg.sql(sql, check=False)
    return json.loads(a.pg.value(sql))


def begin(a):
    key = str(uuid.uuid4())
    return key, call(a, 'begin_version_storage_reconciliation', key)


def siblings(a, count):
    prefix = 'usage-'+uuid.uuid4().hex+'-'
    a.pg.sql(f"""BEGIN;
        INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status)
        SELECT {literal(prefix)}||n::text,'usage-sibling',{literal(a.org)},{literal(a.user)},'ready'
        FROM generate_series(1,{count}) n;
        INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
        SELECT 'pm-'||id,org_id,id,{literal(a.user)},'admin',{literal(a.user)} FROM public.projects
        WHERE id LIKE {literal(prefix+'%')}; COMMIT;
    """)
    return prefix


def test_storage_reconciliation_complete_paging_correction_and_replay(billing):
    a = billing
    siblings(a, 201)
    key, job = begin(a)
    assert job['project_count'] == 202
    after, seen, pages = '', [], []
    while True:
        page = call(a, 'get_version_storage_reconciliation_page', key, after, 200)['projects']
        if not page:
            break
        pages.append(len(page))
        seen.extend(row['project_id'] for row in page)
        assert call(a, 'record_version_storage_measurements', key, {r['project_id']: 0 for r in page}) == {'recorded': len(page)}
        after = seen[-1]
    assert pages == [200, 2] and seen == sorted(set(seen))
    a.pg.sql(f"UPDATE public.organization_usage_counters SET value=99 WHERE org_id={literal(a.org)}")
    result = call(a, 'finish_version_storage_reconciliation', key)
    assert result['value'] == 0 and value(a) == 0 and events(a) == 1
    a.pg.sql(f"UPDATE public.version_repository_billing SET initialized=false WHERE project_id={literal(a.project)};"
             f"DELETE FROM public.organization_entitlements WHERE org_id={literal(a.org)}")
    assert call(a, 'finish_version_storage_reconciliation', key) == result
    assert call(a, 'begin_version_storage_reconciliation', key)['result'] == result
    assert events(a) == 1
    assert a.pg.value(f"SELECT count(*) FROM public.version_storage_reconciliation_projects WHERE reconciliation_id={literal(key)}") == '0'


@pytest.mark.parametrize('change', ['new_project', 'root', 'head', 'generation'])
def test_storage_reconciliation_rejects_concurrent_inventory_changes(billing, change):
    a = billing
    key, _ = begin(a)
    call(a, 'record_version_storage_measurements', key, {a.project: 0})
    if change == 'new_project':
        siblings(a, 1)
    elif change == 'root':
        a.pg.sql('SET ROLE service_role;'+query(a))
    elif change == 'head':
        a.pg.sql(f"UPDATE public.version_repository_refs SET symbolic_target=convert_to('refs/heads/other','UTF8') "
                 f"WHERE project_id={literal(a.project)} AND name=convert_to('HEAD','UTF8')")
    else:
        a.pg.sql(f"UPDATE public.version_repositories SET generation=generation+1 WHERE project_id={literal(a.project)}")
    before = value(a), events(a)
    result = call(a, 'finish_version_storage_reconciliation', key, check=False)
    assert result.returncode and 'storage_reconciliation_snapshot_changed' in result.stderr
    assert (value(a), events(a)) == before
    assert a.pg.value(f"SELECT transaction_xid IS NULL AND result IS NULL FROM public.version_storage_reconciliations WHERE id={literal(key)}") == 't'


@pytest.mark.parametrize('expiry', ['capture', 'entitlement'])
def test_storage_reconciliation_rechecks_expiry_after_counter_wait(billing, expiry):
    a = billing
    key, _ = begin(a)
    call(a, 'record_version_storage_measurements', key, {a.project: 0})
    if expiry == 'capture':
        a.pg.sql(f"UPDATE public.version_storage_reconciliations SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(key)}")
    else:
        a.pg.sql(f"UPDATE public.organization_entitlements SET effective_until=clock_timestamp()+interval '2 seconds' WHERE org_id={literal(a.org)}")
    marker = 'usage-expiry-'+uuid.uuid4().hex
    with ThreadPoolExecutor(1) as pool:
        with transaction(a.pg, f"SELECT 1 FROM public.organization_usage_counters WHERE org_id={literal(a.org)} FOR UPDATE"):
            future = pool.submit(a.pg.sql, f"SET application_name={literal(marker)}; SET ROLE service_role; "
                                 f"SELECT public.finish_version_storage_reconciliation({literal(a.org)},{literal(key)})", check=False)
            wait_for_lock(a.pg, marker)
            time.sleep(2.1)
        result = future.result(timeout=10)
    assert result.returncode and ('storage_reconciliation_expired' if expiry == 'capture' else 'storage_billing_entitlement_invalid') in result.stderr
    assert value(a) == events(a) == 0
    assert a.pg.value(f"SELECT transaction_xid IS NULL AND result IS NULL FROM public.version_storage_reconciliations WHERE id={literal(key)}") == 't'


def test_storage_reconciliation_pruning_is_bounded_and_keeps_receipts(billing):
    a = billing
    siblings(a, 201)
    expired, _ = begin(a)
    refused = call(a, 'begin_version_storage_reconciliation', str(uuid.uuid4()), check=False)
    assert refused.returncode and 'storage_reconciliation_pending' in refused.stderr
    other_project = a.pg.create_project()
    other_org = a.pg.value(f'SELECT org_id FROM public.projects WHERE id={literal(other_project)}')
    a.pg.sql(f"INSERT INTO public.organization_entitlements(org_id,source,source_revision,payload_hash,entitlements) "
             f"SELECT {literal(other_org)},source,source_revision,payload_hash,entitlements "
             f"FROM public.organization_entitlements WHERE org_id={literal(a.org)} "
             'ON CONFLICT(org_id) DO UPDATE SET source=EXCLUDED.source,source_revision=EXCLUDED.source_revision,'
             'payload_hash=EXCLUDED.payload_hash,entitlements=EXCLUDED.entitlements')
    live, _ = begin(SimpleNamespace(pg=a.pg, org=other_org))
    assert call(a, 'cancel_version_storage_reconciliation', expired) is True
    # The scheduler cleanup is global; earlier owned fixtures can have expired
    # captures too. Count them rather than assuming this test owns every row.
    expected = int(a.pg.value('SELECT count(*) FROM public.version_storage_reconciliation_projects p '
                             'JOIN public.version_storage_reconciliations j ON j.id=p.reconciliation_id '
                             'WHERE j.result IS NOT NULL OR j.expires_at<=clock_timestamp()'))
    total = 0
    while True:
        removed = int(a.pg.value('SET ROLE service_role; SELECT public.prune_version_storage_measurements(200)'))
        assert 0 <= removed <= 200
        total += removed
        assert total <= expected
        if not removed:
            break
    assert total == expected
    assert a.pg.value(f"SELECT count(*) FROM public.version_storage_reconciliation_projects WHERE reconciliation_id={literal(live)}") == '1'
    assert a.pg.value(f"SELECT count(*) FROM public.version_storage_reconciliations WHERE id={literal(expired)}") == '1'
    assert call(a, 'begin_version_storage_reconciliation', expired, check=False).returncode


def test_storage_reconciliation_incomplete_malformed_and_old_issuer_fail_closed(billing):
    a = billing
    key, _ = begin(a)
    bad = call(a, 'finish_version_storage_reconciliation', key, check=False)
    assert bad.returncode and 'storage_reconciliation_incomplete' in bad.stderr
    for values in ({a.project: True}, {a.project: -1}, {'foreign': 0}, {}, {a.project: 1}):
        bad = call(a, 'record_version_storage_measurements', key, values, check=False)
        assert bad.returncode
    assert value(a) == events(a) == 0
    for table in ('version_storage_reconciliations', 'version_storage_reconciliation_projects'):
        bad = a.pg.sql(f'SET ROLE service_role; DELETE FROM public.{table}', check=False)
        assert bad.returncode and 'permission denied' in bad.stderr
    a.pg.sql(f"UPDATE public.version_storage_reconciliations SET expires_at=clock_timestamp() WHERE id={literal(key)}")
    bad = call(a, 'record_version_storage_measurements', key, {a.project: 0}, check=False)
    assert bad.returncode and 'storage_reconciliation_expired' in bad.stderr
    assert value(a) == events(a) == 0
