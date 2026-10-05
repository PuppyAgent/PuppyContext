"""No partial inventory settlement or transport materialization in reconciliation."""
import base64
from types import SimpleNamespace

import pytest

from src.platform.billing.storage import StorageReconciliationService
from src.version_engine.infrastructure.supabase.usage_reconciliation import (
    RepositoryUsageReconciler,
)
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.unit.test_repository_snapshot import fixture, row

pytestmark = pytest.mark.hosting_component
KEY = 'bb848e9b-4524-4f2d-b97a-7da8d19042d4'


class Control:
    def __init__(self, count=201):
        self.count, self.calls = count, []
        self.result = None

    def call(self, function, **args):
        self.calls.append((function, args))
        if function == 'begin_version_storage_reconciliation':
            return dict(id=KEY, org_id='org', project_count=self.count, result=self.result)
        if function == 'get_version_storage_reconciliation_page':
            rows = [dict(project_id=f'p{i:06}', state={}, logical_bytes=1) for i in range(self.count)]
            return dict(id=KEY, org_id='org', projects=[r for r in rows if r['project_id'] > args['p_after']][:200])
        if function == 'record_version_storage_measurements':
            return {'recorded': len(args['p_values'])}
        if function == 'finish_version_storage_reconciliation':
            return {'outcome': 'reconciled', 'value': self.count}
        raise AssertionError(function)


def test_usage_reconciliation_pages_and_resumes_without_remeasuring():
    control = Control()
    reconciler = RepositoryUsageReconciler(control, lambda _: pytest.fail('resumed value read objects'))
    assert reconciler.reconcile('org', request_key=KEY)['value'] == 201
    records = [v['p_values'] for f, v in control.calls if f == 'record_version_storage_measurements']
    assert list(map(len, records)) == [200, 1]
    assert len(set().union(*records)) == 201


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'foreign', 'empty', 'receipt'])
def test_usage_reconciliation_never_settles_partial_or_unbound_results(defect):
    control = Control(2)
    real = control.call

    def broken(function, **args):
        value = real(function, **args)
        if function == 'get_version_storage_reconciliation_page':
            if defect == 'missing':
                value['projects'] = value['projects'][:1] if not args['p_after'] else []
            elif defect == 'duplicate':
                value['projects'] *= 2
            elif defect == 'foreign':
                value['org_id'] = 'foreign'
            elif defect == 'empty':
                value['projects'] = []
        elif function == 'record_version_storage_measurements' and defect == 'receipt':
            value['recorded'] = True
        return value

    control.call = broken
    with pytest.raises(RuntimeError):
        RepositoryUsageReconciler(control, None).reconcile('org', request_key=KEY)
    assert not any(f == 'finish_version_storage_reconciliation' for f, _ in control.calls)


def test_usage_reconciliation_replay_requires_valid_result_and_no_storage():
    control = Control()
    control.result = {'outcome': 'reconciled', 'value': 17}
    assert RepositoryUsageReconciler(control, None).reconcile('org', request_key=KEY) == control.result
    assert len(control.calls) == 1
    control.result = {'outcome': 'reconciled', 'value': True}
    with pytest.raises(RuntimeError, match='result'):
        RepositoryUsageReconciler(control, None).reconcile('org', request_key=KEY)


def test_usage_reconciliation_preserves_legacy_read_compatibility():
    blob, raw_blob = encode_object('blob', b'old placement')
    tree, raw_tree = encode_object('tree', b'100644 file\0'+bytes.fromhex(blob))
    objects = {blob: raw_blob, tree: raw_tree}
    backend = SimpleNamespace(publication_project_id='p', get=objects.__getitem__,
                              get_durable=lambda _: pytest.fail('legacy placement treated as native proof'))
    state = dict(authority='legacy', generation=0, object_format='sha1', root=tree, head=None)
    assert RepositoryUsageReconciler(None, lambda _: backend).measure('p', state) == 13


@pytest.mark.parametrize('fmt', ['sha1', 'sha256'])
def test_usage_reconciliation_measures_current_tree_without_history(fmt):
    wire, calls, objects, control, backend = fixture()
    wire['object_format'] = fmt
    blob, objects_raw = encode_object('blob', b'body', object_format=fmt)
    objects[blob] = objects_raw
    tree_body = b'100644 a\0'+bytes.fromhex(blob)+b'120000 b\0'+bytes.fromhex(blob)
    tree, objects_raw = encode_object('tree', tree_body, object_format=fmt)
    objects[tree] = objects_raw
    parent = 'f' * (40 if fmt == 'sha1' else 64)
    body = (f'tree {tree}\nparent {parent}\nauthor T <t@example.test> 1 +0000\n'
            'committer T <t@example.test> 1 +0000\n\nmeasurement\n').encode()
    commit, objects_raw = encode_object('commit', body, object_format=fmt)
    objects[commit] = objects_raw  # The parent is intentionally unavailable.
    wire['refs'].append(row(b'refs/heads/main', {'kind': 'oid', 'oid': commit}, 'commit'))
    state = dict(authority='native', generation=1, object_format=fmt, root=commit,
                 head={'oid': None, 'target': base64.b64encode(b'refs/heads/main').decode()})
    size = len(body)+len(tree_body)+4
    assert RepositoryUsageReconciler(control, lambda _: backend, max_bytes=size, max_objects=3).measure('p', state) == 8
    assert calls.count('get') == 3 and calls[-1] == 'release'
    with pytest.raises(RuntimeError, match='budget'):
        RepositoryUsageReconciler(control, lambda _: backend, max_bytes=size-1).measure('p', state)
    with pytest.raises(RuntimeError, match='budget'):
        RepositoryUsageReconciler(control, lambda _: backend, max_objects=2).measure('p', state)
    assert calls[-1] == 'release'


@pytest.mark.asyncio
async def test_usage_reconciliation_scheduler_has_no_legacy_fallback():
    calls = []

    def fail(org):
        calls.append(org)
        raise RuntimeError('checked snapshot changed')

    checked = SimpleNamespace(prune=lambda: (calls.append('prune') or 0), reconcile=fail)
    usage = SimpleNamespace(claim_reconciliation_batch=lambda **_: ['org'])
    # Legacy collaborators are deliberately unusable, not a recovery route.
    scheduler = StorageReconciliationService(repo_manager=object(), usage_repository=usage,
                                              entitlement_service=object(), project_repository=object(),
                                              checked_reconciler=checked)
    assert await scheduler.reconcile_once(limit=1, min_age_seconds=1) == {'claimed': 1, 'reconciled': 0, 'failed': 1}
    assert calls == ['prune', 'org']
