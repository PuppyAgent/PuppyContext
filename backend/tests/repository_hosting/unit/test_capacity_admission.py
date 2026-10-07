"""Component ordering: reservation failures happen before any physical PUT."""
import asyncio
from types import SimpleNamespace

import pytest

from src.version_engine.derived.repository_gc import RepositoryCollector
from src.version_engine.domain.errors import ObjectNotFoundError, StorageWriteError
from src.version_engine.infrastructure.supabase.capacity_repository import RepositoryCapacity
from src.version_engine.storage.backends.s3 import S3StorageBackend, _run_async
from src.version_engine.storage.mutation_context import publication_storage
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.unit.test_storage_mutation_context import NoIO

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('message', ['PGRST202 missing capacity RPC', 'repository_capacity_exceeded'])
def test_capacity_denial_cannot_upload_or_fall_back(batch, message):
    oid, loose = encode_object('blob', b'abcdef')

    class Client:
        def rpc(self, name, params):
            assert name == 'reserve_version_object_capacity'
            assert params['p_objects'] == [dict(object_id=oid, object_kind='blob', body_bytes=6)]
            assert params['p_required'] is True
            raise RuntimeError(message)

        def table(self, *_args):
            pytest.fail('capacity admission bypassed by direct DML')

    backend = S3StorageBackend(NoIO(), 'project', supabase=SimpleNamespace(client=Client()))
    with publication_storage('project', 'actor', 'pin', require_capacity=True), pytest.raises(StorageWriteError, match=message):
        _run_async(backend.async_put_many({oid: loose}) if batch else backend.async_put(oid, loose))


@pytest.mark.parametrize('value', [None, [], {}, {'new_objects': True, 'new_body_bytes': 6},
                                 {'new_objects': 1, 'new_body_bytes': -1}])
def test_malformed_capacity_ack_is_not_permission_to_put(value):
    class Client:
        def rpc(self, *_args):
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=value))

    backend = S3StorageBackend(NoIO(), 'project', supabase=SimpleNamespace(client=Client()))
    oid, loose = encode_object('blob', b'abcdef')
    with publication_storage('project', 'actor', 'pin', require_capacity=True), pytest.raises(StorageWriteError, match='invalid capacity'):
        _run_async(backend.async_put(oid, loose))


def test_capacity_reservation_precedes_loose_put_and_measures_decoded_body(monkeypatch):
    order = []
    body = b'A' * 4096
    oid, loose = encode_object('blob', body)
    assert len(loose) < 100

    class Client:
        io_id = None

        def rpc(self, name, params):
            if name == 'settle_version_object_capacity_io':
                assert params['p_io_id'] == self.io_id
                order.append('settle')
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=dict(settled_objects=1)))
            assert name == 'reserve_version_object_capacity'
            self.io_id = params['p_io_id']
            assert params['p_objects'][0]['body_bytes'] == len(body)
            order.append('reserve')
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=dict(new_objects=1, new_body_bytes=len(body))))

    backend = S3StorageBackend(NoIO(), 'project', supabase=SimpleNamespace(client=Client()))

    async def put(_key, data, **_kwargs):
        assert data == loose
        order.append('physical')

    monkeypatch.setattr(backend, '_do_put', put)
    with publication_storage('project', 'actor', 'pin', require_capacity=True):
        _run_async(backend.async_put(oid, loose))
    assert order == ['reserve', 'physical', 'settle']


@pytest.mark.asyncio
@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('failure', [TimeoutError, asyncio.CancelledError])
async def test_failed_or_cancelled_storage_does_not_settle_invocation(batch, failure, monkeypatch):
    calls = []

    class Client:
        def rpc(self, name, _params):
            calls.append(name)
            assert name == 'reserve_version_object_capacity'
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=dict(new_objects=2, new_body_bytes=6)))

    async def failed_put(*_args, **_kwargs):
        raise failure('unknown physical outcome')

    storage = SimpleNamespace(upload_file=failed_put)
    storage.for_single_attempt_io = lambda: storage  # Component double has no SDK/retries.
    backend = S3StorageBackend(storage, 'project', supabase=SimpleNamespace(client=Client()))
    monkeypatch.setattr(backend, '_do_put', failed_put)
    objects = dict(encode_object('blob', body) for body in (b'aaa', b'bbb'))
    oid, loose = next(iter(objects.items()))
    with publication_storage('project', 'actor', 'pin', require_capacity=True), pytest.raises(failure):
        await (backend.async_put_many(objects) if batch else backend.async_put(oid, loose))
    assert calls == ['reserve_version_object_capacity']


@pytest.mark.parametrize('response', [None, {}, {'project_id': 'other', 'metric': 'git.object_body_bytes'},
                                    {'project_id': 'project', 'metric': 'storage.logical_bytes'}])
def test_required_capacity_contract_cannot_be_missing_or_repurposed(response):
    capacity = RepositoryCapacity(SimpleNamespace(call=lambda *_args, **_kwargs: response))
    with pytest.raises(RuntimeError, match='capacity contract'):
        capacity.check('project')


@pytest.mark.parametrize('error', [ObjectNotFoundError('absent'), TimeoutError('unknown outcome')])
def test_capacity_only_inventory_needs_explicit_physical_absence(error):
    oid = 'a' * 40
    control = SimpleNamespace(call=lambda *_args, **_kwargs: {
        'objects': {oid: {'created_at': '2026-10-04T00:00:00+00:00', 'unsettled': False}}})

    def get(_oid):
        raise error

    backend = SimpleNamespace(all_hashes_with_metadata=lambda: {}, get_durable=get)
    repo = SimpleNamespace(_project_id='project', store=SimpleNamespace(_backend=backend))
    collector = RepositoryCollector(control)
    if isinstance(error, ObjectNotFoundError):
        metadata, protected = collector._capacity_inventory(repo, 'token', 'sha1')
        assert metadata[oid]['capacity_missing'] is True and protected == ()
    else:
        with pytest.raises(TimeoutError, match='unknown outcome'):
            collector._capacity_inventory(repo, 'token', 'sha1')
