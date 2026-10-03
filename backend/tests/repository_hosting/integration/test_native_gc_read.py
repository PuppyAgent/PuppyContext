import uuid

import pytest

from src.version_engine.derived.object_gc import run_git_object_gc
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.harness.conformance import remote_snapshot
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_transport import (
    native_http as native_http_fixture,
)
from tests.repository_hosting.integration.test_ref_transaction_service import request
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture
native_http = native_http_fixture


@pytest.mark.parametrize("fence", ["gc", "write"])
def test_cold_reader_remains_available_while_writes_are_fenced(native_http, tmp_path, fence):
    source, remote, auth, transport = native_http
    source.run("push", remote, "main")
    before = remote_snapshot(source, remote, tmp_path / "before.git")
    if fence == "gc":
        transport.control.begin_gc(auth.project, str(uuid.uuid4()))
    else:
        auth.pg.sql(f"UPDATE public.version_repositories SET write_state='fenced' WHERE project_id={literal(auth.project)}")
    after = remote_snapshot(source, remote, tmp_path / "after.git")
    assert after == before
    source.commit({"must-not-ack": b"retained locally"})
    assert source.run("push", remote, "main", check=False).returncode != 0
    assert remote_snapshot(source, remote, tmp_path / "after-rejection.git") == before
    assert (source.path / "must-not-ack").read_bytes() == b"retained locally"


@pytest.mark.parametrize("publication", ["sha256"], indirect=True)
def test_sha256_native_gc_quarantine_and_physical_deletion(publication):
    pg, auth, s3, db, backend, service, _git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    orphan, loose = encode_object("blob", b"sha256 quarantine", object_format="sha256")
    backend.put_durable(orphan, loose)
    repo = VersionRepoManager(s3, db).get_server_repo(auth.project, project_name="SHA-256 GC")
    first = run_git_object_gc(repo, dry_run=False, retention_seconds=0, quarantine_seconds=3600)
    assert not first.errors and not first.sweep_skipped_for_safety
    assert first.deleted_count == 0 and first.quarantined_count == 1
    assert backend._inner.exists(orphan)
    pg.sql(f"UPDATE public.version_object_gc_candidates SET first_seen_at=clock_timestamp()-interval '2 hours' WHERE project_id={literal(auth.project)}")
    second = run_git_object_gc(repo, dry_run=False, retention_seconds=0, quarantine_seconds=3600)
    assert not second.errors and second.deleted_sample == [orphan]
    assert not backend._inner.exists(orphan)
    assert ClosureVerifier(backend, object_format="sha256").verify({oid: "commit"})
