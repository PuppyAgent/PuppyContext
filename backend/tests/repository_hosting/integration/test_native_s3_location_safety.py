"""Location-index changes must not invalidate an earlier acknowledged closure."""

from __future__ import annotations

import uuid
from contextvars import copy_context

import pytest

from src.version_engine.derived.object_gc import run_git_object_gc
from src.version_engine.domain.errors import ObjectNotFoundError, StorageWriteError
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.backends.s3 import S3StorageBackend, _run_async
from src.version_engine.storage.mutation_context import collection_storage
from src.version_engine.storage.publication import ClosureVerificationError, ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_ref_transaction_service import request
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_rejected_new_bundle_cannot_break_previously_acknowledged_objects(
    publication, monkeypatch, fault
):
    _pg, auth, s3, db, backend, service, git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    before = service.control.snapshot(auth.project)
    objects = {
        h: encode_object(kind, body, object_format=service.object_format)[1]
        for h, (kind, body) in git.objects().items()
    }
    original_locations = backend._inner._lookup_many_object_locations(list(objects))
    original_keys = {location.pack_key for location in original_locations.values()}
    extra, loose = encode_object(
        "blob", b"extra unreferenced proposal bytes", object_format=service.object_format
    )
    proposal = {**objects, extra: loose}  # Different immutable bundle key.
    uploaded = []
    upload = backend._inner._async_upload_physical_objects

    async def lose_new_bundle(uploads):
        assert not original_keys.intersection(key for key, _body, _mime in uploads)
        await upload(uploads)
        for key, body, mime in uploads:
            uploaded.append(key)
            # Actual owned S3 loss/corruption, before index mutation.
            if fault == "missing":
                await s3.delete_file(key)
            else:
                await s3.upload_file(key, body[:-1] + bytes([body[-1] ^ 1]), content_type=mime)

    def prepare_fault():
        with backend.stage_object_writes() as batch:
            for h, encoded in proposal.items():
                backend.put(h, encoded)
            batch.flush()

    with monkeypatch.context() as patch:
        patch.setattr(backend._inner, "_async_upload_physical_objects", lose_new_bundle)
        with pytest.raises((ClosureVerificationError, StorageWriteError, ObjectNotFoundError)):
            request(service, oid, prepare_fault, target=b"refs/heads/failed-proposal")
    assert uploaded
    assert all(_run_async(s3.file_exists(key)) for key in original_keys)
    after = service.control.snapshot(auth.project)
    assert after["refs"] == before["refs"] and after["ref_sequence"] == before["ref_sequence"]
    fresh = S3StorageBackend(
        s3,
        auth.project,
        supabase=db,
    )
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: "commit"})
    assert {h: fresh.get_durable(h) for h in objects} == objects


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_late_index_completion_after_pin_expiry_cannot_break_newer_ack(publication, monkeypatch):
    pg, auth, s3, db, backend, service, git, oid, prepare = publication
    upsert = backend._inner._async_upsert_object_locations
    pending = {}

    async def suspend_after_first_index_row(rows):
        assert len(rows) > 1
        await upsert(rows[:1])  # An actual partial index, as with a multi-batch upload.
        pending.update(rows=rows[1:], first=rows[0], context=copy_context())
        raise ConnectionError("paused old location writer")

    with monkeypatch.context() as patch:
        patch.setattr(
            backend._inner, "_async_upsert_object_locations", suspend_after_first_index_row
        )
        with pytest.raises(ConnectionError, match="paused old location writer"):
            request(service, oid, prepare)
    assert auth.state() is None
    # Owner-controlled clock-expiry injection in this disposable repository.
    # The old producer still has an outstanding continuation; expiry is not
    # proof of its process or index I/O quiescence.
    pg.sql(
        f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' "
        f"WHERE project_id={literal(auth.project)} AND state='uploading'"
    )
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert not result.errors and not result.sweep_skipped_for_safety
    assert result.deleted_count == 1 and result.deleted_sample == [pending["first"]["object_id"]]
    assert not _run_async(s3.file_exists(pending["first"]["pack_key"]))
    objects = {
        h: encode_object(kind, body, object_format=service.object_format)[1]
        for h, (kind, body) in git.objects().items()
    }

    def prepare_loose():
        for h, encoded in objects.items():
            backend.put_durable(h, encoded)

    assert request(service, oid, prepare_loose)["status"] == "committed"
    before = service.control.snapshot(auth.project)
    fresh = S3StorageBackend(
        s3,
        auth.project,
        supabase=db,
    )
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: "commit"})
    rejected = None
    try:
        pending["context"].run(_run_async, upsert(pending["rows"]))
    except StorageWriteError as exc:
        rejected = exc
    # This cold assertion is essential: unchanged refs alone hide a corrupt
    # location index pointing at the bundle deleted in the preceding epoch.
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: "commit"})
    assert {h: fresh.get_durable(h) for h in objects} == objects
    assert service.control.snapshot(auth.project) == before
    assert rejected is not None


def test_retry_after_sealing_does_not_mutate_sealed_locations(publication, monkeypatch):
    pg, auth, s3, db, _backend, service, _git, oid, prepare = publication
    key = str(uuid.uuid4())

    def fail_before_apply(*_args):
        raise ConnectionError("before ref transaction")

    with monkeypatch.context() as patch:
        patch.setattr(service.control, "apply", fail_before_apply)
        with pytest.raises(ConnectionError, match="before ref transaction"):
            request(service, oid, prepare, key=key)
    assert auth.state() is None
    assert (
        pg.value(
            f"SELECT state FROM public.version_object_pins WHERE project_id={literal(auth.project)}"
        )
        == "verified"
    )

    def forbidden_prepare():
        pytest.fail("sealed pin retried physical preparation")

    result = request(service, oid, forbidden_prepare, key=key)
    assert result["status"] == "committed"
    fresh = S3StorageBackend(
        s3,
        auth.project,
        supabase=db,
    )
    assert ClosureVerifier(fresh).verify({oid: "commit"})
    assert auth.count("version_ref_transactions") == 1


def test_native_gc_rejects_foreign_bundle_without_physical_deletion(publication, monkeypatch):
    pg, auth, s3, db, _backend, service, _git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    other_project = pg.create_project()
    foreign = f"version/{other_project}/object-bundles/ff/{'f' * 64}.pob"
    _run_async(
        s3.upload_file(
            foreign, b"foreign owned-fixture bytes", content_type="application/octet-stream"
        )
    )
    pg.sql(
        "INSERT INTO public.version_object_locations(project_id,object_id,pack_key,offset_bytes,size_bytes) "
        f"VALUES({literal(auth.project)},{literal('d' * 40)},{literal(foreign)},0,26)"
    )

    async def forbidden_delete(*_args, **_kwargs):
        pytest.fail("foreign placement authorized a physical DELETE")

    monkeypatch.setattr(s3, "delete_file", forbidden_delete)
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert result.errors and "outside its canonical Project namespace" in " ".join(result.errors)
    assert result.deleted_count == 0
    assert service.control.snapshot(auth.project)["gc_token"] is not None
    assert _run_async(s3.download_file(foreign)) == b"foreign owned-fixture bytes"
    assert ClosureVerifier(S3StorageBackend(s3, auth.project, supabase=db)).verify({oid: "commit"})


@pytest.mark.parametrize("context", ["absent", "wrong-token", "finished-token", "foreign-project"])
def test_native_physical_delete_requires_current_matching_gc_context(
    publication, monkeypatch, context
):
    _pg, auth, s3, db, backend, service, git, oid, _prepare = publication
    objects = {h: encode_object(kind, body)[1] for h, (kind, body) in git.objects().items()}

    def prepare_loose():
        for h, encoded in objects.items():
            backend.put_durable(h, encoded)

    assert request(service, oid, prepare_loose)["status"] == "committed"
    token = str(uuid.uuid4())
    if context == "finished-token":
        service.control.begin_gc(auth.project, token)
        service.control.finish_gc(auth.project, token)

    async def forbidden_delete(*_args, **_kwargs):
        pytest.fail("uncoordinated physical DELETE was issued")

    monkeypatch.setattr(s3, "delete_file", forbidden_delete)
    with pytest.raises(StorageWriteError, match=r"coordination failed|another Project"):
        if context == "absent":
            backend.delete(oid)
        else:
            with collection_storage(
                "another-project" if context == "foreign-project" else auth.project, token
            ):
                backend.delete(oid)
    fresh = S3StorageBackend(
        s3,
        auth.project,
        supabase=db,
    )
    assert ClosureVerifier(fresh).verify({oid: "commit"})
    assert {h: fresh.get_durable(h) for h in objects} == objects
