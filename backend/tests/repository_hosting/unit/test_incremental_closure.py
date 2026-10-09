"""History-independent physical work and typed aggregate correctness."""

from collections import Counter

import pytest

from src.platform.billing.storage import logical_verified_tree_bytes
from src.version_engine.admission.file_policy import oversized_blob_occurrences
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.storage.publication import ClosureVerificationError, ClosureVerifier
from src.version_engine.write_engine.git_object_format import MODE_DIR, MODE_FILE, TreeEntry
from src.version_engine.write_engine.tree import write_tree

pytestmark = pytest.mark.hosting_component


class ProofFixture:
    """Explicit index substitute; SQL/pin/GC semantics have separate real tests."""

    def __init__(self):
        self.records = {}
        self.lookups = set()

    def prefetch(self, oids):
        self.lookups.update(oids)

    def get(self, oid):
        self.lookups.add(oid)
        return self.records.get(oid)

    def persist(self, manifest):
        self.records.update(manifest.objects)


def commit(store, tree, parent=None, message="change"):
    text = f"tree {tree}\n" + (f"parent {parent}\n" if parent else "")
    return store.put_commit(
        (
            text + "author Test <a@b.test> 1 +0000\ncommitter Test <a@b.test> 1 +0000\n\n" + message
        ).encode()
    )


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_one_file_change_reads_three_new_objects_regardless_of_history(
    tmp_path, monkeypatch, history
):
    store = ObjectStore(tmp_path)
    proof = ProofFixture()
    verifier = ClosureVerifier(store._backend)
    verifier.proofs = proof
    root = write_tree(store, [TreeEntry("a.md", MODE_FILE, store.put_blob(b"old"))])
    head = None
    for _ in range(history):
        head = commit(store, root, head)
    old = verifier.verify({head: "commit"})
    proof.persist(old)
    proof.lookups.clear()
    changed = store.put_blob(b"new content")
    new_root = write_tree(store, [TreeEntry("a.md", MODE_FILE, changed)])
    new_head = commit(store, new_root, head)
    physical = []
    read = store._backend.get_durable

    def counted(oid):
        physical.append(oid)
        return read(oid)

    monkeypatch.setattr(store._backend, "get_durable", counted)
    result = verifier.verify({new_head: "commit"})
    assert Counter(physical) == Counter([new_head, new_root, changed])
    assert len(proof.lookups) == 4  # New commit/tree/blob and one old parent.
    assert set(result.new_objects) == {new_head, new_root, changed}
    assert logical_verified_tree_bytes(result, new_root) == len(b"new content")
    assert not oversized_blob_occurrences(result, new_root, 100)


def test_shared_subtrees_keep_logical_multiplicity_and_grandfather_counts(tmp_path):
    store = ObjectStore(tmp_path)
    proof = ProofFixture()
    verifier = ClosureVerifier(store._backend)
    verifier.proofs = proof
    blob = store.put_blob(b"oversized")
    subtree = write_tree(store, [TreeEntry("file", MODE_FILE, blob)])
    original = write_tree(store, [TreeEntry("a", MODE_DIR, subtree)])
    proof.persist(verifier.verify({original: "tree"}))
    copied = write_tree(
        store, [TreeEntry("a", MODE_DIR, subtree), TreeEntry("b", MODE_DIR, subtree)]
    )
    result = verifier.verify({copied: "tree"})
    assert result.new_objects == (copied,)
    assert logical_verified_tree_bytes(result, copied) == 2 * len(b"oversized")
    assert oversized_blob_occurrences(result, copied, 2) == {blob: 2}


def test_new_bytes_cannot_use_cache_or_wrong_type_as_integrity_proof(tmp_path):
    store = ObjectStore(tmp_path)
    proof = ProofFixture()
    verifier = ClosureVerifier(store._backend)
    verifier.proofs = proof
    blob = store.put_blob(b"correct")
    manifest = verifier.verify({blob: "blob"})
    proof.persist(manifest)
    with pytest.raises(ClosureVerificationError, match="type mismatch"):
        verifier.verify({blob: "tree"})
    proof.records.clear()
    store._backend._path_for(blob).write_bytes(b"broken")
    with pytest.raises(ClosureVerificationError, match="cannot verify"):
        verifier.verify({blob: "blob"})


def test_incremental_tree_summary_rejects_overflow(tmp_path):
    store = ObjectStore(tmp_path)
    proof = ProofFixture()
    verifier = ClosureVerifier(store._backend)
    verifier.proofs = proof
    blob = store.put_blob(b"x")
    tree = write_tree(store, [TreeEntry("file", MODE_FILE, blob)])
    for _ in range(63):
        tree = write_tree(store, [TreeEntry("a", MODE_DIR, tree), TreeEntry("b", MODE_DIR, tree)])
    with pytest.raises(ClosureVerificationError, match="counter range"):
        verifier.verify({tree: "tree"})


def test_concurrent_publications_do_not_share_pin_bound_proof_caches(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from types import SimpleNamespace
    from uuid import uuid4

    from src.version_engine.write_engine import ref_transaction

    owner = local()
    ready = Barrier(2)
    checked = []
    store = ObjectStore(tmp_path)
    blobs = [store.put_blob(value) for value in (b"first", b"second")]

    class BoundProof(ProofFixture):
        def __init__(self, *args):
            super().__init__()
            self.pin = owner.pin = args[3]

        def get(self, oid):
            assert self.pin == owner.pin
            return super().get(oid)

        def persist(self, manifest):
            assert self.pin == owner.pin
            checked.append((self.pin, tuple(manifest.roots)))

    control = SimpleNamespace(
        result=lambda *args: None,
        snapshot=lambda *args: {
            "authority": "native",
            "write_state": "active",
            "object_format": "sha1",
            "generation": 1,
        },
        begin=lambda *args: {"state": "uploading"},
        seal=lambda *args: None,
        apply=lambda *args: {"status": "committed"},
        release=lambda *args: None,
    )
    monkeypatch.setattr(ref_transaction, "admitted_actor", lambda *args, **kwargs: "user:test")
    service = ref_transaction.RefTransactionService(
        control,
        store._backend,
        project_id="project",
        proof_factory=BoundProof,
    )

    def publish(oid):
        return service.submit(
            object(),
            request_key=str(uuid4()),
            generation=1,
            edits=[
                ref_transaction.RefEdit(
                    b"refs/tags/" + oid.encode(),
                    ref_transaction.RefState(),
                    ref_transaction.RefState(oid=oid),
                )
            ],
            roots={oid: "blob"},
            prepare=lambda: ready.wait(timeout=5),
        )

    with ThreadPoolExecutor(2) as pool:
        assert all(row["status"] == "committed" for row in pool.map(publish, blobs))
    assert len({pin for pin, _ in checked}) == 2
    assert {root for _, (root,) in checked} == set(blobs)
    assert service.verifier.proofs is None
