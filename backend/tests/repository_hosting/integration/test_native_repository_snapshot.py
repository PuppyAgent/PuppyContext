"""Admitted read/base snapshots on actual PG/S3; not canonical API admission."""
from __future__ import annotations

import uuid

import pytest

from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState
from tests.repository_hosting.integration.test_ref_transaction_service import grant, request
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_snapshot_resolves_exact_declared_base_without_transport(publication, monkeypatch):
    _pg, auth, _s3, _db, backend, service, git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    tree, blob = git.text("rev-parse", "HEAD^{tree}"), git.text("rev-parse", "HEAD:original.txt")
    import subprocess
    def no_transport(*_args, **_kwargs):
        pytest.fail("a product read must not materialize a transport repository")
    monkeypatch.setattr(subprocess, "run", no_transport)
    monkeypatch.setattr(subprocess, "Popen", no_transport)
    with repository_snapshot(service.control, backend, grant(auth.project), project_id=auth.project) as snapshot:
        revision = snapshot.revision()
        assert (snapshot.object_format, revision.commit_oid, revision.tree_oid) == (service.object_format, oid, tree)
        assert revision.ref_name == b"refs/heads/main"
        with pytest.raises(PermissionError, match="not reachable"):
            snapshot.object(blob)
        assert snapshot.object(tree)[0] == "tree"
        assert snapshot.object(blob) == ("blob", b"original\n")
        with pytest.raises(TypeError):
            snapshot.refs[b"HEAD"] = RefState(oid=oid)
        with pytest.raises(KeyError, match="does not exist"):
            snapshot.revision(b"refs/heads/missing")
        assert snapshot.revision(b"refs/heads/missing", allow_absent=True).commit_oid is None
    with pytest.raises(RuntimeError, match="closed"):
        snapshot.object(blob)


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_default_branch_change_rejects_saved_base_even_when_commit_is_unchanged(publication):
    _pg, auth, _s3, _db, backend, service, _git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    with repository_snapshot(service.control, backend, grant(auth.project), project_id=auth.project) as snapshot:
        revision = snapshot.revision()
        moved = service.submit(
            grant(auth.project), request_key=str(uuid.uuid4()), generation=snapshot.generation,
            edits=[RefEdit(b"refs/heads/topic", RefState(), RefState(oid=oid)),
                   RefEdit(b"HEAD", RefState(target=b"refs/heads/main"), RefState(target=b"refs/heads/topic"))],
            roots={oid: "commit"}, prepare=lambda: None,
        )
        assert moved["status"] == "committed"
        before = service.control.snapshot(auth.project)
        rejected = service.submit(
            grant(auth.project), request_key=str(uuid.uuid4()), generation=snapshot.generation,
            edits=revision.edit(oid), roots={oid: "commit"}, prepare=lambda: None,
        )
        assert rejected["status"] == "rejected"
        after = service.control.snapshot(auth.project)
        assert (before["refs"], before["ref_sequence"]) == (after["refs"], after["ref_sequence"])
        assert snapshot.revision() == revision
        assert revision.expected == RefState(oid=oid)


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_unborn_detached_and_typed_tag_snapshots(publication):
    _pg, auth, _s3, _db, backend, service, git, oid, prepare = publication
    with repository_snapshot(service.control, backend, grant(auth.project), project_id=auth.project) as snapshot:
        unborn = snapshot.revision()
        assert unborn.commit_oid is None and unborn.expected == RefState()
        assert unborn.tree_oid == encode_object("tree", b"", object_format=service.object_format)[0]
    assert request(service, oid, prepare)["status"] == "committed"
    raw = f"object {oid}\ntype commit\ntag release\n\nrelease\n".encode()
    tag, loose = encode_object("tag", raw, object_format=service.object_format)
    assert service.submit(
        grant(auth.project), request_key=str(uuid.uuid4()), generation=1,
        edits=[RefEdit(b"refs/tags/release", RefState(), RefState(oid=tag)),
               RefEdit(b"HEAD", RefState(target=b"refs/heads/main"), RefState(oid=oid))],
        roots={tag: "tag", oid: "commit"}, prepare=lambda: backend.put(tag, loose),
    )["status"] == "committed"
    with repository_snapshot(service.control, backend, grant(auth.project), project_id=auth.project) as snapshot:
        head = snapshot.revision()
        assert head.ref_name == b"HEAD" and head.head_guard is None
        assert head.edit(oid) == (RefEdit(b"HEAD", RefState(oid=oid), RefState(oid=oid)),)
        tagged = snapshot.revision(b"refs/tags/release")
        assert tagged.expected == RefState(oid=tag)
        assert tagged.commit_oid == oid and tagged.tree_oid == git.text("rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_read_snapshot_blocks_new_gc_and_does_not_expose_rejected_roots(publication):
    _pg, auth, _s3, _db, backend, service, _git, oid, prepare = publication
    assert request(service, oid, prepare)["status"] == "committed"
    rejected_oid, loose = encode_object("blob", b"unpublished proposal", object_format=service.object_format)
    assert service.submit(
        grant(auth.project), request_key=str(uuid.uuid4()), generation=1,
        edits=[RefEdit(b"refs/tags/rejected", RefState(oid="f" * len(oid)), RefState(oid=rejected_oid))],
        roots={rejected_oid: "blob"}, prepare=lambda: backend.put(rejected_oid, loose),
    )["status"] == "rejected"
    token = str(uuid.uuid4())
    with repository_snapshot(service.control, backend, grant(auth.project), project_id=auth.project) as snapshot:
        with pytest.raises(Exception, match="publication_in_progress"):
            service.control.begin_gc(auth.project, token)
        with pytest.raises(PermissionError, match="not reachable"):
            snapshot.object(rejected_oid)
        assert snapshot.revision().commit_oid == oid
    service.control.begin_gc(auth.project, token)
    # No worker or DELETE was dispatched by this admission-only probe.
    service.control.finish_gc(auth.project, token)
