"""Actual S3-compatible service + production adapter + PostgREST/PG refs.

Synthetic admitted grant/OIDs are not end-user authorization evidence. Objects,
location metadata, publication pins, receipts and ref writes are not mocked.
"""

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from supabase import ClientOptions, create_client

from src.version_engine.derived.object_gc import run_git_object_gc
from src.version_engine.derived.object_gc_worker import process_object_gc_projects
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.backends.s3 import CachedStorageBackend, S3StorageBackend
from src.version_engine.storage.mutation_context import publication_storage
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1, encode_object
from src.version_engine.write_engine.ref_transaction import RefTransactionService, admitted_actor
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import Authority
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_ref_transaction_service import grant, request

pytestmark = pytest.mark.hosting_s3


@pytest.fixture
def publication(pg_project, tmp_path, request):
    object_format = getattr(request, "param", "sha1")
    pg, project = pg_project
    pg.sql(
        f"UPDATE public.projects SET version_root_hash={literal(EMPTY_TREE_SHA1)} WHERE id={literal(project)}"
    )
    auth = Authority(
        pg,
        project,
        object_format=object_format,
        roots={"a" * (64 if object_format == "sha256" else 40): "commit"},
    )
    pg.sql(f"DELETE FROM public.version_publication_receipts WHERE project_id={literal(project)}")
    with owned_s3() as (s3, api), httpx.Client(timeout=15, trust_env=False) as http:
        sdk = create_client(
            str(api.client.base_url),
            api.headers("service_role")["apikey"],
            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
        )
        db = SimpleNamespace(client=sdk)
        physical = S3StorageBackend(
            s3,
            project,
            supabase=db,
        )
        backend = CachedStorageBackend(physical)
        service = RefTransactionService(
            RefAuthorityRepository(sdk), backend, project_id=project, object_format=object_format
        )
        git = Git.init(tmp_path / "client", format=object_format)
        oid = git.commit({"original.txt": b"original\n", "binary": bytes(range(256))})
        objects = git.objects()

        def prepare():
            with backend.stage_object_writes() as batch:
                for oid, (kind, body) in objects.items():
                    backend.put(oid, encode_object(kind, body, object_format=object_format)[1])
                batch.flush()  # Real POB/index path, not only loose objects.

        yield pg, auth, s3, db, backend, service, git, oid, prepare


@contextmanager
def seed_objects(publication, roots):
    """Explicit pinned fixture uploads, without a receipt or readable ref root."""
    service = publication[5]
    actor = admitted_actor(grant(service.project_id), service.project_id, write=True)
    pin = str(uuid.uuid4())
    service.control.begin(service.project_id, actor, pin, 1, roots)
    with publication_storage(service.project_id, actor, pin):
        yield
    service.control.release(service.project_id, actor, pin)


def test_publication_survives_fresh_s3_backend_and_cold_native_fsck(publication, tmp_path):
    pg, auth, s3, db, _backend, service, git, oid, prepare = publication
    result = request(service, oid, prepare)
    assert result["status"] == "committed"
    assert auth.state()["oid"] == oid
    fresh = S3StorageBackend(
        s3,
        auth.project,
        supabase=db,
    )
    manifest = ClosureVerifier(fresh).verify({oid: "commit"})
    receipt = json.loads(
        pg.value(
            f"SELECT row_to_json(r) FROM public.version_publication_receipts r WHERE id={literal(result['receipt_id'])}"
        )
    )
    assert receipt["manifest_sha256"] == manifest.digest
    cold = Git.init(tmp_path / "cold.git", bare=True)
    for expected, (kind, raw) in git.objects().items():
        assert fresh.get_durable(expected) == encode_object(kind, raw)[1]
        assert (
            cold.run("hash-object", "-w", "-t", kind, "--stdin", input=raw).stdout.strip().decode()
            == expected
        )
    cold.run("update-ref", "refs/heads/main", oid)
    cold.run("fsck", "--full", "--strict")
    assert cold.objects() == git.objects()
    assert cold.refs() == git.refs()


def test_physical_s3_loss_cannot_be_hidden_by_process_cache(publication):
    _pg, auth, s3, _db, backend, service, _git, oid, prepare = publication
    # Populate the cache/index without publishing, then delete the physical pack.
    with seed_objects(publication, {oid: "commit"}):
        prepare()
    assert backend.get(oid)
    physical = backend._inner
    location = physical._lookup_object_location(oid)
    assert location is not None
    s3.client.delete_object(Bucket=s3.bucket_name, Key=location.pack_key)
    assert backend.get(oid)  # Known hot-cache false durability signal.
    with pytest.raises(RuntimeError, match="cannot verify"):
        request(service, oid, lambda: None)
    assert auth.state() is None
    assert auth.count("version_publication_receipts") == 0
    assert auth.count("version_ref_transactions") == 0


def test_s3_outage_does_not_publish_or_acknowledge(publication, monkeypatch):
    _pg, auth, s3, _db, _backend, service, _git, oid, prepare = publication
    # Connection failure against an unused loopback port, not an S3 mock.
    import boto3
    from botocore.config import Config

    failed = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        region_name="local",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(proxies={}, connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}),
    )
    try:
        with monkeypatch.context() as m:
            m.setattr(s3, "client", failed)
            with pytest.raises((EndpointConnectionError, StorageWriteError)):
                request(service, oid, prepare)
        assert auth.state() is None
        assert auth.count("version_publication_receipts") == 0
        assert request(service, oid, prepare)["status"] == "committed"
    finally:
        failed.close()


def test_gc_preserves_verified_refs_and_reclaims_actual_s3_orphan(publication):
    _pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    request(service, oid, prepare)
    orphan, loose = encode_object("blob", b"unacknowledged orphan")
    backend.put_durable(orphan, loose)
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert not result.errors and not result.sweep_skipped_for_safety
    assert result.deleted_count == 1 and result.deleted_sample == [orphan]
    assert not backend._inner.exists(orphan)
    assert ClosureVerifier(backend).verify({oid: "commit"})
    assert service.control.snapshot(auth.project)["gc_token"] is None


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_gc_worker_uses_native_inventory_without_legacy_current_tree_access(publication):
    pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    request(service, oid, prepare)
    manager = VersionRepoManager(s3, db)
    assert not hasattr(manager, "get_server_repo")
    before = service.control.snapshot(auth.project)
    results = process_object_gc_projects(
        repo_manager=manager,
        client=db.client,
        project_ids=[auth.project],
        dry_run=True,
        retention_seconds=0,
    )
    assert len(results) == 1
    result = results[0]
    assert result.project_id == auth.project and result.dry_run
    assert not result.errors and not result.sweep_skipped_for_safety
    assert result.deleted_count == 0 and result.reachable_count > 0
    assert service.control.snapshot(auth.project) == before
    assert ClosureVerifier(backend, object_format=service.object_format).verify({oid: "commit"})
    assert (
        pg.value(
            f"SELECT count(*) FROM public.version_object_gc_runs WHERE project_id={literal(auth.project)} AND dry_run"
        )
        == "1"
    )
    assert not hasattr(manager, "_cache")


def test_gc_dry_run_does_not_advance_epoch_or_leave_a_sweep_fence(publication):
    _pg, auth, s3, db, _backend, service, _git, oid, prepare = publication
    request(service, oid, prepare)
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    before = service.control.snapshot(auth.project)
    result = run_git_object_gc(repo, dry_run=True, retention_seconds=0)
    assert not result.errors and result.deleted_count == 0
    assert service.control.snapshot(auth.project) == before


def test_gc_cannot_delete_old_objects_while_a_new_publication_is_pinned(publication):
    _pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)

    def overlap():
        prepare()
        result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
        assert result.sweep_skipped_for_safety and result.deleted_count == 0
        assert "publication_in_progress" in " ".join(result.errors)

    assert request(service, oid, overlap)["status"] == "committed"
    assert ClosureVerifier(backend).verify({oid: "commit"})


def test_unknown_s3_delete_result_keeps_fence_instead_of_racing_next_writer(
    publication, monkeypatch
):
    _pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    request(service, oid, prepare)
    orphan, loose = encode_object("blob", b"unknown delete result")
    backend.put_durable(orphan, loose)
    original = s3.delete_file

    async def lost_ack(key):
        await original(key)
        raise ConnectionError("lost DELETE acknowledgement")

    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    with monkeypatch.context() as m:
        m.setattr(s3, "delete_file", lost_ack)
        result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert result.errors
    assert service.control.snapshot(auth.project)["gc_token"] is not None
    with pytest.raises(Exception, match="repository_gc_in_progress"):
        request(service, oid, prepare, target=b"refs/heads/new")
    assert ClosureVerifier(backend).verify({oid: "commit"})
