"""Native Product splices over actual PG/S3; synthetic enrollment, not HTTP writes."""

import asyncio
import importlib
import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.product.tree_patch import splice_batch
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.infrastructure.supabase.file_policy_repository import RepositoryFilePolicy
from src.version_engine.read.native_tree_reader import NativeTreeReader
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
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


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
async def test_native_s3_product_splice_publish_and_read_only_replay(publication, monkeypatch):
    pg, auth, s3, db, backend, _, git, _, _ = publication
    capacity, grant, org = enroll(publication)
    billing = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    enroll_file_policy(billing, 512)
    control = capacity.control
    service = RefTransactionService(control, backend, project_id=auth.project,
                                    object_format=capacity.object_format, capacity=capacity.capacity,
                                    billing=RepositoryBilling(control), policy=RepositoryFilePolicy(control))
    # Import inside the test so absent implementation is a case-level red.
    module = importlib.import_module("src.version_engine.write_engine.native_operation_writer")
    writer = module.NativeOperationWriter(service)
    with repository_snapshot(control, backend, grant, project_id=auth.project) as snapshot:
        base = NativeTreeReader(snapshot).get_read_revision(auth.project)
    key = str(uuid.uuid4())
    raw_name = b"raw-\xff".decode("utf-8", "surrogateescape")
    def splice(store, root):
        return splice_batch(store, root, [("put", "one.md", b"first"), ("put", raw_name, b"raw")])
    async with ProjectWriteLease(auth.project, "native-product", repository=ProjectWriteLeaseRepository(db.client)):
        first = await asyncio.to_thread(writer.apply, grant, request_key=key, base=base,
                                        input_sha256="1"*64, splice=splice, message="Product save")
    assert first["status"] == "committed"
    assert value(billing) == 8 and events(billing) == 1
    acknowledged = control.snapshot(auth.project)
    oid = auth.state()["oid"]
    fresh = S3StorageBackend(s3, auth.project, supabase=db, allow_deferred_namespace_reads=False)
    def verify():
        with repository_snapshot(control, fresh, grant, project_id=auth.project) as snapshot:
            reader = NativeTreeReader(snapshot)
            assert reader.read_file(auth.project, "one.md") == b"first"
            assert reader.read_file(auth.project, raw_name) == b"raw"
        closure = ClosureVerifier(fresh, object_format=service.object_format).verify({oid: "commit"})
        # Stock Git checks native-produced objects, not a Git-produced candidate.
        for h, obj in closure.objects.items():
            kind, body = fresh_object(fresh, h, service.object_format)
            assert kind == obj.kind
            assert git.run("hash-object", "-t", kind, "-w", "--stdin", input=body).stdout.decode().strip() == h
        git.run("update-ref", "refs/heads/product", oid)
        git.run("fsck", "--full", "--strict")
    await asyncio.to_thread(verify)
    assert pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(auth.project)}") == "0"
    # The committed retry must not allocate a pin, PUT, recompute a splice,
    # require a write lease, or consult a newly missing entitlement.
    pg.sql(f"UPDATE public.version_repository_file_policies SET initialized=false WHERE project_id={literal(auth.project)};"
           f"DELETE FROM public.organization_entitlements WHERE org_id={literal(org)};"
           f"UPDATE public.access_surface_credentials SET grant_mode='r' WHERE id={literal(grant.principal.principal_id)}")
    before_pins = pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE project_id={literal(auth.project)}")
    monkeypatch.setattr(backend, "put_durable", lambda *args: pytest.fail("replay attempted PUT"))
    reader_grant = RuntimeGrant(grant.principal, grant.target, grant.repository_view, RuntimeMode.READ)
    replay = await asyncio.to_thread(writer.apply, reader_grant, request_key=key, base=base,
                                    input_sha256="1"*64, splice=lambda *_: pytest.fail("replay spliced"), message="Product save")
    assert replay == first and value(billing) == 8 and events(billing) == 1
    assert pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE project_id={literal(auth.project)}") == before_pins
    assert control.snapshot(auth.project) == acknowledged
    with pytest.raises(Exception, match="request_key_reused"):
        await asyncio.to_thread(writer.apply, reader_grant, request_key=key, base=base,
                                input_sha256="2"*64, splice=splice, message="Product save")


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
@pytest.mark.parametrize("failure", ["before_ref", "after_ref"])
async def test_native_s3_product_sealed_and_lost_ack_recovery(publication, monkeypatch, failure):
    pg, auth, _s3, db, backend, _, _git, _, _prepare = publication
    capacity, grant, org = enroll(publication)
    billing = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    enroll_file_policy(billing, 512)
    control = capacity.control
    service = RefTransactionService(control, backend, project_id=auth.project,
                                    object_format=capacity.object_format, capacity=capacity.capacity,
                                    billing=RepositoryBilling(control), policy=RepositoryFilePolicy(control))
    module = importlib.import_module("src.version_engine.write_engine.native_operation_writer")
    writer = module.NativeOperationWriter(service)
    with repository_snapshot(control, backend, grant, project_id=auth.project) as snapshot:
        base = NativeTreeReader(snapshot).get_read_revision(auth.project)
    key = str(uuid.uuid4())
    original = service.policy.apply
    def fault(*args, **kwargs):
        if failure == "after_ref":
            original(*args, **kwargs)
        raise RuntimeError("simulated ref boundary interruption")
    monkeypatch.setattr(service.policy, "apply", fault)
    async with ProjectWriteLease(auth.project, "product-first", repository=ProjectWriteLeaseRepository(db.client)):
        with pytest.raises(RuntimeError, match="simulated ref boundary interruption"):
            await asyncio.to_thread(writer.apply, grant, request_key=key, base=base, input_sha256="a"*64,
                                    splice=lambda store, root: splice_batch(store, root, [("put", "file", b"data")]))
    assert value(billing) == (4 if failure == "after_ref" else 0)
    monkeypatch.setattr(service.policy, "apply", original)
    monkeypatch.setattr(backend, "put_durable", lambda *_: pytest.fail("sealed/replayed operation attempted PUT"))
    request = dict(request_key=key, base=base, input_sha256="a"*64,
                   splice=lambda *_: pytest.fail("sealed/replayed operation reran splice"))
    if failure == "before_ref":
        async with ProjectWriteLease(auth.project, "product-resume", repository=ProjectWriteLeaseRepository(db.client)):
            result = await asyncio.to_thread(writer.apply, grant, **request)
    else:
        result = await asyncio.to_thread(writer.apply, grant, **request)
    assert result["status"] == "committed" and value(billing) == 4 and events(billing) == 1
    assert pg.value(f"SELECT count(*) FROM public.version_ref_transactions WHERE project_id={literal(auth.project)}") == "1"


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
async def test_native_s3_product_same_oid_head_switch_rejects_old_provenance(publication):
    pg, auth, _s3, db, backend, _, _git, _, _prepare = publication
    capacity, grant, org = enroll(publication)
    billing = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    enroll_file_policy(billing, 512)
    service = RefTransactionService(capacity.control, backend, project_id=auth.project,
                                    object_format=capacity.object_format, capacity=capacity.capacity,
                                    billing=RepositoryBilling(capacity.control), policy=RepositoryFilePolicy(capacity.control))
    module = importlib.import_module("src.version_engine.write_engine.native_operation_writer")
    writer = module.NativeOperationWriter(service)
    def capture():
        with repository_snapshot(service.control, backend, grant, project_id=auth.project) as snapshot:
            return NativeTreeReader(snapshot).get_read_revision(auth.project)
    base = await asyncio.to_thread(capture)
    async with ProjectWriteLease(auth.project, "product-head-guard", repository=ProjectWriteLeaseRepository(db.client)):
        first = await asyncio.to_thread(writer.apply, grant, request_key=str(uuid.uuid4()), base=base,
                                        input_sha256="c"*64,
                                        splice=lambda store, root: splice_batch(store, root, [("put", "file", b"data")]))
        captured = await asyncio.to_thread(capture)
        oid = first["product"]["commit_oid"]
        result = await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                         edits=[RefEdit(b"refs/heads/other", RefState(), RefState(oid=oid))],
                                         roots={oid: "commit"}, prepare=lambda: None)
        assert result["status"] == "committed"
        selected = module.NativeWriteBase.parse(captured)
        result = await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                         edits=[RefEdit(b"HEAD", selected.head_guard, RefState(target=b"refs/heads/other"))],
                                         roots={}, prepare=lambda: pytest.fail("metadata HEAD switch uploaded"))
        assert result["status"] == "committed"
        before = service.control.snapshot(auth.project)
        with pytest.raises(module.NativeRevisionConflictError, match="HEAD changed"):
            await asyncio.to_thread(writer.apply, grant, request_key=str(uuid.uuid4()), base=captured,
                                    input_sha256="d"*64, splice=lambda *_: pytest.fail("stale HEAD ran splice"))
    assert service.control.snapshot(auth.project) == before and value(billing) == 4


def fresh_object(backend, oid, object_format):
    from src.version_engine.write_engine.git_object_format import decode_object
    kind, body = decode_object(backend.get_durable(oid))
    assert encode_object(kind, body, object_format=object_format)[0] == oid
    return kind, body
