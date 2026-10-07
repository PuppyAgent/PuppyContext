"""Actual S3/PG file limits; synthetic grants/projections, not public HTTP auth."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.git.native_repository import NativeGitRepository
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.infrastructure.supabase.file_policy_repository import RepositoryFilePolicy
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_capacity import capacity_usage, enroll
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
async def test_native_s3_file_policy_grandfather_copy_named_refs_and_rejected_proofs(
    publication, monkeypatch
):
    pg, auth, s3, db, backend, _original, git, first_oid, prepare = publication
    capacity, grant, org = enroll(publication)
    fixture = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    pg.sql(
        f'UPDATE public.organization_entitlements SET entitlements=\'{{"limits":{{"storage.max_bytes":10000}}}}\' WHERE org_id={literal(org)}'
    )
    enroll_file_policy(fixture, 512)
    control = capacity.control
    service = RefTransactionService(
        control,
        backend,
        project_id=auth.project,
        object_format=capacity.object_format,
        capacity=capacity.capacity,
        billing=RepositoryBilling(control),
        policy=RepositoryFilePolicy(control),
    )
    first_args = dict(
        request_key=str(uuid.uuid4()),
        generation=1,
        edits=[RefEdit(b"refs/heads/main", RefState(), RefState(oid=first_oid))],
        roots={first_oid: "commit"},
        prepare=prepare,
    )

    def upload():
        with backend.stage_object_writes() as batch:
            for oid, (kind, body) in git.objects().items():
                backend.put(oid, encode_object(kind, body, object_format=service.object_format)[1])
            batch.flush()

    async def publish(new, old, prepare=upload, ref=b"refs/heads/main", kind="commit"):
        return await asyncio.to_thread(
            service.submit,
            grant,
            request_key=str(uuid.uuid4()),
            generation=1,
            edits=[RefEdit(ref, RefState(oid=old), RefState(oid=new))],
            roots={new: kind},
            prepare=prepare,
        )

    async with ProjectWriteLease(
        auth.project, "native-file-policy", repository=ProjectWriteLeaseRepository(db.client)
    ):
        first = await asyncio.to_thread(service.submit, grant, **first_args)
        assert first["status"] == "committed" and value(fixture) == 265
        advertised = await asyncio.to_thread(
            NativeGitRepository(service).info_refs, grant, "git-upload-pack"
        )
        assert first_oid.encode() in advertised.body
        # Store a novel large blob in a rejected transaction under the earlier
        # valid plan. Its allocation/receipt must not grandfather a future ACK.
        orphan, loose = encode_object("blob", b"orphan" * 60, object_format=service.object_format)
        rejected = await publish(
            orphan,
            first_oid,
            lambda: backend.put_durable(orphan, loose),
            b"refs/tags/orphan",
            "blob",
        )
        assert rejected["status"] == "rejected"
        pg.sql(
            f"UPDATE public.organization_entitlements SET source_revision=2,entitlements=jsonb_set(entitlements,"
            f"'{{limits,upload.max_single_file_bytes}}','16') WHERE org_id={literal(org)}"
        )
        baseline = control.snapshot(auth.project)
        with pytest.raises(PermissionError, match="file_size_limit_exceeded"):
            await publish(
                orphan,
                None,
                lambda: backend.put_durable(orphan, loose),
                b"refs/tags/orphan",
                "blob",
            )
        assert control.snapshot(auth.project)["refs"] == baseline["refs"]
        # Rename preserves oversized occurrence counts. Existing history and
        # typed blob tags remain usable after a plan downgrade.
        renamed = git.commit({"binary": None, "renamed": bytes(range(256))})
        moved = await publish(renamed, first_oid)
        assert moved["status"] == "committed"
        blob = git.text("rev-parse", "HEAD:renamed")
        tagged = await publish(blob, None, lambda: None, b"refs/tags/old-blob", "blob")
        assert tagged["status"] == "committed"
        before_copy = control.snapshot(auth.project)
        copied = git.commit({"copy": bytes(range(256))})
        copy_args = dict(
            request_key=str(uuid.uuid4()),
            generation=1,
            edits=[RefEdit(b"refs/heads/main", RefState(oid=renamed), RefState(oid=copied))],
            roots={copied: "commit"},
            prepare=upload,
        )
        with pytest.raises(PermissionError, match="file_size_limit_exceeded"):
            await asyncio.to_thread(service.submit, grant, **copy_args)
        assert control.snapshot(auth.project)["refs"] == before_copy["refs"]
        assert control.snapshot(auth.project)["ref_sequence"] == before_copy["ref_sequence"]
        assert value(fixture) == 265 and events(fixture) == 2
        deltas = json.loads(
            pg.value(
                f"SELECT jsonb_agg(delta ORDER BY created_at,id) FROM public.organization_usage_events WHERE org_id={literal(org)}"
            )
        )
        assert deltas == [265, 0]  # Rename is a new default version, not a new charge.
        # A newly introduced oversized object is rejected before even one
        # single-attempt PUT; spy on the actual isolated SDK mutation client.
        physical = s3.for_single_attempt_io().client
        original_put, puts = physical.put_object, []

        def record_put(**kwargs):
            puts.append(kwargs["Key"])
            return original_put(**kwargs)

        monkeypatch.setattr(physical, "put_object", record_put)
        used = capacity_usage(pg, auth.project)
        new_blob, encoded = encode_object("blob", b"N" * 4096, object_format=service.object_format)
        with pytest.raises(Exception, match="file_size_limit_exceeded"):
            await publish(
                new_blob,
                None,
                lambda: backend.put_durable(new_blob, encoded),
                b"refs/tags/new-blob",
                "blob",
            )
        assert puts == [] and capacity_usage(pg, auth.project) == used
        assert control.snapshot(auth.project)["refs"] == before_copy["refs"]
        fresh = S3StorageBackend(
            s3,
            auth.project,
            supabase=db,
        )
        proof = ClosureVerifier(fresh, object_format=service.object_format).verify(
            {renamed: "commit", blob: "blob"}
        )
        for oid, record in proof.objects.items():
            kind, body = git.objects()[oid]
            assert (
                record.kind == kind
                and fresh.get_durable(oid)
                == encode_object(kind, body, object_format=service.object_format)[1]
            )
        assert value(fixture) == 265
    # A sealed, physically completed attempt can recover under a new lease and
    # a newly acknowledged plan; no old worker gains new uploading authority.
    pg.sql(
        f"UPDATE public.organization_entitlements SET source_revision=3,entitlements=jsonb_set(entitlements,"
        f"'{{limits,upload.max_single_file_bytes}}','512') WHERE org_id={literal(org)}"
    )
    copy_args["prepare"] = lambda: pytest.fail("sealed retry attempted PUT")
    async with ProjectWriteLease(
        auth.project, "native-file-retry", repository=ProjectWriteLeaseRepository(db.client)
    ):
        recovered = await asyncio.to_thread(service.submit, grant, **copy_args)
        assert recovered["status"] == "committed" and auth.state()["oid"] == copied
        assert value(fixture) == 521 and events(fixture) == 3
        assert ClosureVerifier(fresh, object_format=service.object_format).verify(
            {copied: "commit"}
        )
    pg.sql(
        f"UPDATE public.version_repository_file_policies SET initialized=false WHERE project_id={literal(auth.project)};"
        f"DELETE FROM public.organization_entitlements WHERE org_id={literal(org)}"
    )
    reader = RuntimeGrant(grant.principal, grant.target, grant.repository_view, RuntimeMode.READ)
    first_args["prepare"] = lambda: pytest.fail("replay attempted storage mutation")
    assert await asyncio.to_thread(service.submit, reader, **first_args) == first
    assert value(fixture) == 521 and events(fixture) == 3
    pg.sql(
        f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(grant.principal.principal_id)}"
    )
    with pytest.raises(Exception, match="repository_action_denied"):
        await asyncio.to_thread(NativeGitRepository(service).info_refs, reader, "git-upload-pack")
