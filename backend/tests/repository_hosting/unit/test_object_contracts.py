import random

import pytest

from src.version_engine.derived.object_gc import _child_object_ids
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import (
    TreeEntry,
    decode_commit,
    decode_object,
    decode_tree,
    encode_object,
    encode_tree,
)

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("size", [0, 1, 63, 4096, 1_048_576])
@pytest.mark.parametrize("seed", [0, 17, 999])
def test_arbitrary_blobs_roundtrip_against_git(tmp_path, git_repo, size, seed):
    body = random.Random(seed).randbytes(size)
    oid, loose = encode_object("blob", body)
    assert oid == git_repo.run("hash-object", "--stdin", input=body).stdout.decode().strip()
    store = ObjectStore(tmp_path / "objects")
    store.put_loose(oid, loose)
    reopened = ObjectStore(tmp_path / "objects")
    assert decode_object(reopened.get_loose(oid)) == ("blob", body)


@pytest.mark.parametrize("name", ["a", "目录", "é", "e\u0301", "a b", "a\nb", "-option"])
@pytest.mark.parametrize("mode", [b"100644", b"100755", b"120000"])
def test_tree_bytes_match_git(git_repo, name, mode):
    oid = git_repo.run("hash-object", "-w", "--stdin", input=b"payload").stdout.strip()
    body = encode_tree([TreeEntry(name, mode, oid.decode())])
    native = git_repo.run(
        "mktree", "-z", input=mode + b" blob " + oid + b"\t" + name.encode() + b"\0"
    ).stdout.strip()
    assert native.decode() == encode_object("tree", body)[0]
    assert decode_tree(body) == [TreeEntry(name, mode, oid.decode())]


def test_corrupt_loose_object_is_rejected_on_verified_read(tmp_path):
    from src.version_engine.domain.errors import ObjectNotFoundError

    store = ObjectStore(tmp_path / "objects")
    oid, _loose = encode_object("blob", b"original")
    _other, changed = encode_object("blob", b"changed")
    store.put_loose(oid, changed)  # Inject damaged physical storage, bypass typed writers.
    with pytest.raises(ObjectNotFoundError, match="corrupt"):
        store.get_object(oid)
    with pytest.raises(ObjectNotFoundError, match="corrupt"):
        store.get_objects_many([oid])


@pytest.mark.parametrize("parents", [0, 1, 2, 4])
def test_decode_preserves_parent_order_and_raw_commit(git_repo, parents):
    tree = git_repo.run("mktree", input=b"").stdout.strip().decode()
    roots = [
        git_repo.run("commit-tree", tree, input=f"root {n}\n".encode()).stdout.strip().decode()
        for n in range(parents)
    ]
    args = [arg for oid in roots for arg in ("-p", oid)]
    oid = git_repo.run("commit-tree", tree, *args, input=b"message\n").stdout.strip().decode()
    raw = git_repo.run("cat-file", "commit", oid).stdout
    assert decode_commit(raw)["parents"] == roots
    assert encode_object("commit", raw)[0] == oid


def test_raw_non_utf8_tree_name_is_lossless():
    body = b"100644 bad-\xff\x00" + bytes.fromhex("12" * 20)
    assert encode_tree(decode_tree(body)) == body


def test_gc_tag_target_is_a_reachable_child():
    oid = "12" * 20
    raw = f"object {oid}\ntype commit\ntag v1\ntagger T <t@example.test> 0 +0000\n\ntag\n".encode()
    assert _child_object_ids("tag", raw) == [oid]


def test_gc_does_not_require_external_submodule_commit():
    body = encode_tree([TreeEntry("external", b"160000", "12" * 20)])
    assert _child_object_ids("tree", body) == []
