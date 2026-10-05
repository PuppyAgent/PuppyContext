"""Independent Product invocations over real PG/S3, not process/restore acceptance."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ReadTimeoutError

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.product.tree_patch import splice_batch
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.native_tree_reader import NativeTreeReader
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.native_operation_writer import NativeOperationWriter
from src.version_engine.write_engine.ref_transaction import admitted_actor
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_capacity import enroll
from tests.repository_hosting.integration.test_repository_file_policy import enroll_file_policy
from tests.repository_hosting.integration.test_repository_logical_billing import (
    enroll_billing,
    events,
    value,
)
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


def setup(publication):
    pg, auth, s3, db, *_ = publication
    _, grant, org = enroll(publication)
    billing = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    enroll_file_policy(billing, 512)
    manager = VersionRepoManager(s3, db)
    service = manager.get_native_service(auth.project)
    with repository_snapshot(service.control, service.backend, grant, project_id=auth.project) as snapshot:
        base = NativeTreeReader(snapshot).get_read_revision(auth.project)
    request = dict(request_key=str(uuid.uuid4()), base=base, input_sha256='b'*64, message='stable retry',
                   splice=lambda store, root: splice_batch(store, root, [('put', 'file', b'data')]))
    return pg, auth, s3, db, grant, billing, manager, service, request


def pending(pg, project):
    return json.loads(pg.value(f"SELECT coalesce(jsonb_agg(to_jsonb(i) ORDER BY io_id),'[]') "
        f"FROM public.version_repository_capacity_inflight i WHERE project_id={literal(project)}"))


def prepared(pg, project):
    return json.loads(pg.value(f"SELECT to_jsonb(o) FROM public.version_product_operations o WHERE project_id={literal(project)}"))


def read_bytes(service, grant):
    with repository_snapshot(service.control, service.backend, grant, project_id=service.project_id) as snapshot:
        return NativeTreeReader(snapshot).read_file(service.project_id, 'file')


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
@pytest.mark.parametrize('expire_pin', [False, True])
async def test_native_product_new_attempt_preserves_old_unknown_io(publication, monkeypatch, expire_pin):
    pg, auth, s3, db, grant, billing, manager, service, request = setup(publication)
    writer = NativeOperationWriter(service)
    blob = encode_object('blob', b'data', object_format=service.object_format)[0]
    key = service.backend._key_for(blob)
    strict = s3.for_single_attempt_io().client
    send = strict._endpoint.http_session.send
    lost = []
    def lose_one_ack(outbound):
        response = send(outbound)
        if outbound.method == 'PUT' and urlsplit(outbound.url).path.endswith('/' + key) and not lost:
            assert response.status_code == 200
            lost.append(True)
            response.raw.close()
            raise ReadTimeoutError(endpoint_url=s3.endpoint_url)
        return response
    monkeypatch.setattr(strict._endpoint.http_session, 'send', lose_one_ack)
    async with ProjectWriteLease(auth.project, 'product-interrupted', repository=ProjectWriteLeaseRepository(db.client)):
        with pytest.raises(StorageWriteError, match='Read timeout'):
            await asyncio.to_thread(writer.apply, grant, **request)
    original = prepared(pg, auth.project)
    old_claims = pending(pg, auth.project)
    assert len(old_claims) == 1 and value(billing) == 0
    old_pin = old_claims[0]['pin_id']
    status = await asyncio.to_thread(manager.get_native_operation_status, auth.project, grant, request['request_key'])
    assert status['status'] == 'pending' and status['product'] is None and status['result'] is None
    assert status['input_sha256'] == original['input_sha256']
    assert pending(pg, auth.project) == old_claims
    if expire_pin:
        pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(old_pin)}")
    # Fresh service/control/backend, genuine original base and candidate clock.
    resumed = manager.get_native_service(auth.project)
    async with ProjectWriteLease(auth.project, 'product-independent', repository=ProjectWriteLeaseRepository(db.client)):
        result = await asyncio.to_thread(NativeOperationWriter(resumed).apply, grant, **request)
    assert result['status'] == 'committed' and result['receipt_id'] != old_pin
    assert result['product']['commit_oid'] == original['proposal']['product_result']['commit_oid']
    assert prepared(pg, auth.project) == original
    assert pending(pg, auth.project) == old_claims  # Another worker must not settle it.
    assert value(billing) == 4 and events(billing) == 1
    # The winning physical attempt differs from the original preparation. Query
    # its canonical result with ALL S3 I/O unavailable, without original bytes.
    def forbidden_s3(*_args, **_kwargs):
        raise AssertionError('operation lookup must not access S3')
    with monkeypatch.context() as patch:
        patch.setattr(s3.client._endpoint.http_session, 'send', forbidden_s3)
        patch.setattr(strict._endpoint.http_session, 'send', forbidden_s3)
        status = await asyncio.to_thread(manager.get_native_operation_status, auth.project, grant, request['request_key'])
    assert status['result'] == {k: v for k, v in result.items() if k != 'product'}
    assert status['product'] == result['product'] and status['input_sha256'] == original['input_sha256']
    assert prepared(pg, auth.project) == original and pending(pg, auth.project) == old_claims
    assert value(billing) == 4 and events(billing) == 1
    assert await asyncio.to_thread(read_bytes, resumed, grant) == b'data'
    assert pg.value(f"SELECT state FROM public.version_object_pins WHERE id={literal(old_pin)}") == 'released'
    assert strict.meta.config.retries == {'total_max_attempts': 1, 'mode': 'standard'}
    # This observer saw that exact request finish. Only now settle its claim;
    # elapsed time, expiry and success of the second invocation were not proof.
    resumed.control.call('settle_version_object_capacity_io', p_project_id=auth.project,
        p_actor=admitted_actor(grant, auth.project, write=False), p_pin_id=old_pin, p_io_id=old_claims[0]['io_id'])
    assert pending(pg, auth.project) == []


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_pre_attempt_native_fixed_pin_sealed_receipt_is_reused(publication, monkeypatch):
    pg, auth, _s3, db, grant, billing, manager, service, request = setup(publication)
    # Exercise the prior native preparation protocol, not legacy authority or an
    # old application binary: use its original fixed pin without attempt metadata.
    def original_preparation(project, actor, key, digest, generation, attempt):
        return service.control.read_product_operation(project, actor, key, digest, generation)
    def stop_before_ref(*args, **kwargs):
        raise RuntimeError('sealed original native pin')
    monkeypatch.setattr(service.control, 'open_product_attempt', original_preparation)
    monkeypatch.setattr(service.policy, 'apply', stop_before_ref)
    async with ProjectWriteLease(auth.project, 'product-original-pin', repository=ProjectWriteLeaseRepository(db.client)):
        with pytest.raises(RuntimeError, match='sealed original native pin'):
            await asyncio.to_thread(NativeOperationWriter(service).apply, grant, **request)
    original = prepared(pg, auth.project)
    pin = original['proposal']['receipt_id']
    assert pg.value(f"SELECT state FROM public.version_object_pins WHERE id={literal(pin)}") == 'verified'
    assert pg.value(f"SELECT count(*) FROM public.version_product_publication_attempts WHERE project_id={literal(auth.project)}") == '0'
    resumed = manager.get_native_service(auth.project)
    monkeypatch.setattr(resumed.backend, 'put_durable', lambda *_: pytest.fail('sealed original pin attempted PUT'))
    request['splice'] = lambda *_: pytest.fail('sealed original pin attempted splice')
    async with ProjectWriteLease(auth.project, 'product-reuse-original-pin', repository=ProjectWriteLeaseRepository(db.client)):
        result = await asyncio.to_thread(NativeOperationWriter(resumed).apply, grant, **request)
    assert result['status'] == 'committed' and result['receipt_id'] == pin
    assert result['product']['commit_oid'] == original['proposal']['product_result']['commit_oid']
    assert prepared(pg, auth.project) == original and pending(pg, auth.project) == []
    assert value(billing) == 4 and events(billing) == 1
    assert await asyncio.to_thread(read_bytes, resumed, grant) == b'data'


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_late_old_product_put_cannot_damage_new_attempt_ack(publication, monkeypatch):
    pg, auth, s3, db, grant, billing, manager, service, request = setup(publication)
    blob = encode_object('blob', b'data', object_format=service.object_format)[0]
    key = service.backend._key_for(blob)
    strict = s3.for_single_attempt_io().client
    send = strict._endpoint.http_session.send
    started, release = threading.Event(), threading.Event()
    old_response = []
    intercepted = []
    def delay_one(outbound):
        if outbound.method == 'PUT' and urlsplit(outbound.url).path.endswith('/' + key) and not intercepted:
            intercepted.append(True)
            started.set()
            assert release.wait(20), 'test did not release its owned old worker'
            response = send(outbound)  # Real late PUT after the other attempt's ACK.
            old_response.append(response.status_code)
            return response
        return send(outbound)
    monkeypatch.setattr(strict._endpoint.http_session, 'send', delay_one)
    async def first_worker():
        async with ProjectWriteLease(auth.project, 'product-delayed', repository=ProjectWriteLeaseRepository(db.client)):
            return await asyncio.to_thread(NativeOperationWriter(service).apply, grant, **request)
    old_worker = asyncio.create_task(first_worker())
    try:
        assert await asyncio.to_thread(started.wait, 10), 'old request did not reach physical PUT'
        claims = pending(pg, auth.project)
        assert len(claims) == 1
        old_pin = claims[0]['pin_id']
        pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' "
               f"WHERE id=(SELECT lease_id FROM public.version_publication_admissions WHERE pin_id={literal(old_pin)})")
        resumed = manager.get_native_service(auth.project)
        async with ProjectWriteLease(auth.project, 'product-after-expiry', repository=ProjectWriteLeaseRepository(db.client)):
            acknowledged = await asyncio.to_thread(NativeOperationWriter(resumed).apply, grant, **request)
        assert acknowledged['status'] == 'committed' and acknowledged['receipt_id'] != old_pin
        assert pending(pg, auth.project) == claims
        assert await asyncio.to_thread(read_bytes, resumed, grant) == b'data'
        release.set()
        assert await old_worker == acknowledged  # Current-read recovery, not a second publication.
        assert old_response == [200]
        assert await asyncio.to_thread(read_bytes, manager.get_native_service(auth.project), grant) == b'data'
        assert value(billing) == 4 and events(billing) == 1
        assert pg.value(f"SELECT count(*) FROM public.version_ref_transactions WHERE project_id={literal(auth.project)}") == '1'
    finally:
        primary = sys.exception()
        release.set()
        try:
            await old_worker  # Never leave the owned worker or physical request in flight.
        except Exception as cleanup_error:
            if primary is None:
                raise
            primary.add_note(f"Owned old worker also failed during cleanup: {cleanup_error}")
