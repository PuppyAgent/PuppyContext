"""Actual chunked-object durability and late physical-upload isolation.

Small configured chunks exercise layout races; this is not a large-object
memory/performance or independent-process recovery acceptance receipt.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from src.version_engine.derived.object_gc import run_git_object_gc
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.backends.s3 import S3StorageBackend, _run_async
from src.version_engine.storage.io_strategy import IOStorageStrategy
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_ref_transaction_service import request
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
    seed_objects,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_native_chunked_publication_survives_late_different_chunk_boundaries(publication):
    _pg, auth, s3, db, backend, service, git, oid, prepare = publication
    backend._inner._io_strategy = IOStorageStrategy(128, 64)
    old_producer = S3StorageBackend(s3, auth.project, supabase=db,
                                   allow_deferred_namespace_reads=False,
                                   io_strategy=IOStorageStrategy(128, 32))
    blob = git.text("rev-parse", "HEAD:binary")
    raw = git.run("cat-file", "blob", blob).stdout
    encoded = encode_object("blob", raw, object_format=service.object_format)[1]
    pending, _old_location = old_producer._chunked_object_upload_plan(blob, encoded)
    assert request(service, oid, prepare)["status"] == "committed"
    before = auth.state()
    # Model previously issued old-part PUTs completing after the newer ACK,
    # without their manifest completing. No location-index write is performed.
    _run_async(old_producer._async_upload_physical_objects([
        item for item in pending if item[2] != "application/json"
    ]))
    fresh = S3StorageBackend(s3, auth.project, supabase=db, allow_deferred_namespace_reads=False)
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: "commit"})
    assert fresh.get_durable(blob) == encoded
    assert auth.state() == before


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_native_gc_removes_immutable_chunk_orphan_and_keeps_published_closure(publication):
    _pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    backend._inner._io_strategy = IOStorageStrategy(128, 64)
    orphan, loose = encode_object("blob", bytes(range(256)) * 3, object_format=service.object_format)
    with seed_objects(publication, {orphan: "blob"}):
        backend.put_durable(orphan, loose)
    location = backend._inner._lookup_object_location(orphan)
    assert location.pack_key.startswith("chunked:")
    keys = backend._inner._chunked_keys_for(
        orphan, location.pack_key.removeprefix("chunked:"), location.size_bytes,
    )
    assert all(_run_async(s3.file_exists(key)) for key in keys)
    before = service.control.snapshot(auth.project)
    repo = VersionRepoManager(s3, db).get_server_repo(auth.project, project_name="Chunk GC")
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert not result.errors and not result.sweep_skipped_for_safety
    assert result.deleted_count == 1 and result.deleted_sample == [orphan]
    assert all(not _run_async(s3.file_exists(key)) for key in keys)
    after = service.control.snapshot(auth.project)
    assert after["refs"] == before["refs"] and after["ref_sequence"] == before["ref_sequence"]
    assert after["gc_token"] is None
    fresh = S3StorageBackend(s3, auth.project, supabase=db, allow_deferred_namespace_reads=False)
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: "commit"})


@pytest.mark.parametrize("fault", ["invalid-json", "foreign-part"])
def test_native_gc_retains_fence_without_any_delete_for_invalid_manifest(publication, monkeypatch, fault):
    pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    backend._inner._io_strategy = IOStorageStrategy(128, 64)
    orphan, loose = encode_object("blob", bytes(range(256)) * 3)
    with seed_objects(publication, {orphan: "blob"}):
        backend.put_durable(orphan, loose)
    location = backend._inner._lookup_object_location(orphan)
    key = location.pack_key.removeprefix("chunked:")
    if fault == "invalid-json":
        raw = b"not JSON"
    else:
        manifest = json.loads(_run_async(s3.download_file(key)))
        manifest["chunks"][-1]["key"] = manifest["chunks"][-1]["key"].replace(auth.project, "foreign-project")
        raw = json.dumps(manifest).encode()
        key = key.rsplit("manifest-", 1)[0] + "manifest-" + hashlib.sha256(raw).hexdigest() + ".json"
    _run_async(s3.upload_file(key, raw, content_type="application/json"))
    # Owner-injected corrupt metadata in an owned synthetic repository, not a
    # permission granted to the runtime principal or an end-user write path.
    pg.sql(f"UPDATE public.version_object_locations SET pack_key={literal('chunked:' + key)} "
           f"WHERE project_id={literal(auth.project)} AND object_id={literal(orphan)}")
    deletions = []
    original_delete = s3.delete_file

    async def observe_delete(key):
        deletions.append(key)
        return await original_delete(key)

    monkeypatch.setattr(s3, "delete_file", observe_delete)
    before = service.control.snapshot(auth.project)
    repo = VersionRepoManager(s3, db).get_server_repo(auth.project, project_name="Invalid chunk GC")
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert result.errors and result.deleted_count == 0
    assert not deletions
    after = service.control.snapshot(auth.project)
    assert after["gc_token"] is not None
    assert after["refs"] == before["refs"] and after["ref_sequence"] == before["ref_sequence"]
    fresh = S3StorageBackend(s3, auth.project, supabase=db, allow_deferred_namespace_reads=False)
    assert ClosureVerifier(fresh).verify({oid: "commit"})
