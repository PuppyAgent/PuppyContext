"""Product tree primitives -> pinned publication -> cold stock Git.

This is storage/codec interoperability with an explicit fixture grant, not
canonical ProductOperationAdapter admission, billing, Scope or API acceptance.
"""
from __future__ import annotations

import subprocess
import uuid

import pytest

from src.platform.billing.storage import logical_tree_delta
from src.version_engine.adapters.product.tree_patch import splice_put_blob
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.storage.object_store import ObjectStore
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.integration.test_native_s3_transport import (
    native_http as native_http_fixture,
)
from tests.repository_hosting.integration.test_ref_transaction_service import grant
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture
native_http = native_http_fixture


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_product_splice_publishes_exact_native_tree_without_transport_materialization(
    publication, native_http, tmp_path, monkeypatch,
):
    _pg, auth, _s3, _db, backend, service, client, _oid, _prepare = publication
    _source, remote, _auth, _transport = native_http
    blob = client.text("rev-parse", "HEAD:original.txt")
    external = "f" * len(blob)
    for mode, oid, name in [
        ("100755", blob, "executable"), ("120000", blob, "link"),
        ("160000", external, "external"), ("100644", blob, "opaque-\udcff"),
        ("100755", blob, "nested/executable"), ("100644", blob, "nested/edit"),
    ]:
        client.run("update-index", "--add", "--cacheinfo", mode, oid, name)
    client.run("commit", "-m", "native entry modes")
    old = client.text("rev-parse", "HEAD")
    old_tree = client.text("rev-parse", "HEAD^{tree}")
    client.run("push", remote, "main")

    content = b"product edit\n"
    incoming = client.run("hash-object", "-w", "--stdin", input=content).stdout.decode().strip()
    client.run("update-index", "--cacheinfo", "100644", incoming, "nested/edit")
    expected_tree = client.text("write-tree")
    raw_commit = (
        f"tree {expected_tree}\nparent {old}\n"
        "author Product <product@example.test> 1700000000 +0000\n"
        "committer Product <product@example.test> 1700000000 +0000\n\nProduct save\n"
    ).encode()
    expected_commit = client.run("hash-object", "-w", "-t", "commit", "--stdin", input=raw_commit).stdout.decode().strip()

    def no_git(*args, **kwargs):
        raise AssertionError("product writer must not materialize a transport repository")

    unused = tmp_path / "no-product-transport"
    with (repository_snapshot(service.control, backend, grant(auth.project), project_id=auth.project) as snapshot,
          monkeypatch.context() as patch, backend.stage_object_writes() as batch):
        revision = snapshot.revision()
        assert (revision.commit_oid, revision.tree_oid) == (old, old_tree)
        patch.setattr(subprocess, "run", no_git)
        patch.setattr(subprocess, "Popen", no_git)
        store = ObjectStore(unused, backend, object_format=snapshot.object_format)
        new_tree, changes = splice_put_blob(store, revision.tree_oid, "nested/edit", content)
        assert changes == [("update", "nested/edit")]
        assert new_tree == expected_tree
        assert logical_tree_delta(store, old_tree, new_tree) == len(content) - len(b"original\n")
        new_commit = store.put_commit(raw_commit)
        assert new_commit == expected_commit
        # All product writes above are staged. RefTransactionService acquires
        # its publication pin before flushing, then verifies physical closure.
        result = service.submit(
            grant(auth.project), request_key=str(uuid.uuid4()), generation=snapshot.generation,
            edits=revision.edit(new_commit),
            roots={new_commit: "commit"}, prepare=batch.flush, message="product primitive interoperability",
        )
    assert result["status"] == "committed"
    assert auth.state()["oid"] == expected_commit
    assert not unused.exists()
    client.run("update-ref", "refs/heads/main", expected_commit)
    client.run("clone", "--mirror", remote, tmp_path / "cold.git")
    cold = Git(tmp_path / "cold.git")
    cold.run("fsck", "--full", "--strict")
    assert cold.refs() == {"refs/heads/main": expected_commit}
    assert cold.objects() == client.objects()
