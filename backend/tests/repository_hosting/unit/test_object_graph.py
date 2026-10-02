"""Byte/graph regressions against stock Git, not full hosting acceptance."""

from types import SimpleNamespace

import pytest

from src.version_engine.adapters.git import object_quarantine
from src.version_engine.derived.object_gc import mark_reachable_objects, run_git_object_gc
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import (
    TreeEntry,
    decode_commit,
    decode_tag,
    decode_tree,
    encode_object,
    encode_tree,
)
from src.version_engine.write_engine.git_object_graph import object_edges
from src.version_engine.write_engine.tree_objects import find_missing_tree_objects
from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_component
OID = "12" * 20


def put(store, kind, body):
    oid, loose = encode_object(kind, body)
    store.put_loose(oid, loose)
    return oid


def tag(target, kind, message=b"message\n"):
    return (
        f"object {target}\ntype {kind}\ntag v1\n"
        "tagger Test <test@example.test> 1767225600 +0000\n\n"
    ).encode() + message


def test_byte_names_sort_like_native_git(git_repo):
    blob = git_repo.text("hash-object", "-w", "--stdin")
    empty = git_repo.text("mktree")
    entries = [
        TreeEntry(name.decode("utf-8", "surrogateescape"), mode, oid)
        for name, mode, oid in [
            (b"\xff", b"100644", blob), (b"\xc3\xa9", b"100644", blob),
            (b"\x80", b"120000", blob), (b"a", b"40000", empty),
            (b"a.c", b"100644", blob), (b"a0", b"100644", blob),
        ]
    ]
    native_input = b"".join(
        entry.mode + (b" tree " if entry.is_dir else b" blob ")
        + entry.sha1_hex.encode() + b"\t"
        + entry.name.encode("utf-8", "surrogateescape") + b"\0"
        for entry in entries
    )
    native = git_repo.run("mktree", "-z", input=native_input).stdout.strip().decode()
    body = encode_tree(entries)
    assert encode_object("tree", body)[0] == native
    assert encode_tree(decode_tree(body)) == body
    assert git_repo.run("cat-file", "tree", native).stdout == body


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\0b"])
def test_encoder_rejects_invalid_tree_components(name):
    with pytest.raises(ValueError):
        encode_tree([TreeEntry(name, b"100644", OID)])


@pytest.mark.parametrize("oid", [" " * 40, "12" * 19 + "  ", "g" * 40, "12" * 19])
def test_encoder_rejects_invalid_oid_bytes(oid):
    with pytest.raises(ValueError):
        encode_tree([TreeEntry("file", b"100644", oid)])


def test_duplicate_tree_names_are_not_a_valid_graph():
    entry = b"100644 file\0" + bytes.fromhex(OID)
    with pytest.raises(ValueError, match="duplicate"):
        object_edges("tree", entry + entry)


def test_non_utf8_commit_headers_do_not_hide_graph_edges():
    parent = "34" * 20
    raw = (
        f"tree {OID}\nparent {parent}\nparent {OID}\n".encode()
        + b"author Ren\xe9 <r@example.test> 0 +0000\n"
        + b"encoding ISO-8859-1\ngpgsig signature\n tree not-a-real-header\n\nmsg\xff\n"
    )
    assert [(edge.oid, edge.kind) for edge in object_edges("commit", raw)] == [
        (OID, "tree"), (parent, "commit"), (OID, "commit"),
    ]
    assert [edge.oid for edge in object_edges("commit", raw, follow_history=False)] == [OID]
    assert decode_commit(raw)["parents"] == [parent, OID]
    assert decode_commit(raw)["message"].encode("utf-8", "surrogateescape") == b"msg\xff"


@pytest.mark.parametrize("kind", ["blob", "tree", "commit", "tag"])
def test_tag_edges_preserve_target_type_and_non_utf8_message(kind):
    raw = tag(OID, kind, b"signed-\xff\n")
    assert [(edge.oid, edge.kind) for edge in object_edges("tag", raw)] == [(OID, kind)]
    assert decode_tag(raw)["object"] == OID


@pytest.mark.parametrize("kind,body", [
    ("commit", b"parent " + OID.encode() + b"\n\nno tree\n"),
    ("commit", f"tree {OID}\ntree {OID}\n\n".encode()),
    ("commit", f"tree {OID}\nparent garbage\n\n".encode()),
    ("tag", f"object {OID}\ntype unknown\n\n".encode()),
    ("tag", f"object {OID}\nobject {OID}\ntype blob\n\n".encode()),
    ("tag", b"object " + b"0" * 40 + b"\ntype blob\n\n"),
    ("tree", b"999999 file\0" + bytes.fromhex(OID)),
    ("tree", b"100644 file\0" + bytes.fromhex(OID)[:-1]),
    ("unknown", b"anything"),
])
def test_malformed_graph_fails_closed(kind, body):
    with pytest.raises(ValueError):
        object_edges(kind, body)


def test_submodule_is_not_a_local_object_dependency(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    body = encode_tree([TreeEntry("external", b"160000", OID)])
    tree = store.put_tree(body)
    assert not store.exists(OID)
    assert object_edges("tree", body) == []
    assert find_missing_tree_objects(store, tree) == []
    errors = []
    assert mark_reachable_objects(SimpleNamespace(store=store), [tree], errors=errors) == {tree}
    assert errors == []


@pytest.mark.parametrize("target_kind", ["blob", "tree", "commit"])
def test_nested_tag_closure_materializes_as_native_git(tmp_path, git_repo, target_kind):
    git_repo.commit({"file": b"body"})
    # A gitlink deliberately names an object absent from this repository.
    git_repo.run("update-index", "--add", "--cacheinfo", f"160000,{OID},submodule")
    git_repo.run("commit", "-m", "external gitlink")
    target = {
        "commit": "HEAD", "tree": "HEAD^{tree}", "blob": "HEAD:file",
    }[target_kind]
    target_oid = git_repo.text("rev-parse", target)
    inner = git_repo.run("hash-object", "-w", "-t", "tag", "--stdin",
                         input=tag(target_oid, target_kind)).stdout.strip().decode()
    outer = git_repo.run("hash-object", "-w", "-t", "tag", "--stdin",
                         input=tag(inner, "tag")).stdout.strip().decode()
    git_repo.run("update-ref", "refs/tags/outer", outer)
    store = ObjectStore(tmp_path / "canonical")
    for oid, (kind, body) in git_repo.objects().items():
        assert put(store, kind, body) == oid
    repo = SimpleNamespace(store=store)
    restored = Git.init(tmp_path / "restored.git", bare=True)
    object_quarantine.copy_reachable_objects_to_bare(repo, restored.path, [outer])
    restored.run("update-ref", "refs/tags/outer", outer)
    restored.run("fsck", "--full", "--strict")
    errors = []
    expected = mark_reachable_objects(repo, [outer], errors=errors)
    assert not errors
    assert OID not in expected
    assert set(restored.objects()) == expected
    for oid in expected:
        assert restored.objects()[oid] == git_repo.objects()[oid]


def test_fallback_graph_matches_native_rev_list(tmp_path, git_repo, monkeypatch):
    git_repo.commit({"file": b"body"})
    git_repo.run("update-index", "--add", "--cacheinfo", f"160000,{OID},submodule")
    commit = git_repo.text("write-tree")
    tagged = git_repo.run("hash-object", "-w", "-t", "tag", "--stdin",
                         input=tag(commit, "tree")).stdout.strip().decode()
    expected = object_quarantine._reachable_object_ids_from_bare(git_repo.path / ".git", [tagged])
    monkeypatch.setattr(object_quarantine, "_rev_list_object_ids_from_bare", lambda *a, **k: None)
    assert object_quarantine._reachable_object_ids_from_bare(git_repo.path / ".git", [tagged]) == expected
    assert OID not in expected


def test_unreadable_fallback_graph_is_not_silently_truncated(git_repo, monkeypatch):
    monkeypatch.setattr(object_quarantine, "_rev_list_object_ids_from_bare", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="required Git object"):
        object_quarantine._reachable_object_ids_from_bare(git_repo.path / ".git", [OID])


def test_malformed_tag_blocks_destructive_gc(component_repo):
    repo = component_repo.repo
    root = put(repo.store, "tag", tag(OID, "unknown"))
    orphan = repo.store.put_blob(b"must remain until reachability is proven")
    repo.history.list_version_ref_roots = lambda: [root]
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert result.sweep_skipped_for_safety
    assert result.deleted_count == 0
    assert result.errors
    assert repo.store.exists(orphan)


def test_failed_cache_materialization_cannot_skip_missing_children_on_retry(tmp_path):
    store = ObjectStore(tmp_path / "canonical")
    tree = store.put_tree(encode_tree([TreeEntry("missing", b"100644", OID)]))
    root = put(store, "tag", tag(tree, "tree"))
    bare = Git.init(tmp_path / "retry.git", bare=True)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="missing"):
            object_quarantine.copy_reachable_objects_to_bare(SimpleNamespace(store=store), bare.path, [root])
    assert not (bare.path / "puppyone-closure").exists()


def test_blobless_copy_cannot_certify_full_closure(tmp_path):
    store = ObjectStore(tmp_path / "canonical")
    tree = store.put_tree(encode_tree([TreeEntry("missing", b"100644", OID)]))
    bare = Git.init(tmp_path / "partial.git", bare=True)
    repo = SimpleNamespace(store=store)
    object_quarantine.copy_reachable_objects_to_bare(repo, bare.path, [tree], include_blobs=False)
    with pytest.raises(RuntimeError, match="missing"):
        object_quarantine.copy_reachable_objects_to_bare(repo, bare.path, [tree], include_blobs=True)


def test_unverified_truncated_cache_object_is_rebuilt(tmp_path):
    store = ObjectStore(tmp_path / "canonical")
    blob = store.put_blob(b"complete blob")
    root = put(store, "tag", tag(blob, "blob"))
    bare = Git.init(tmp_path / "truncated.git", bare=True)
    cached = bare.path / "objects" / root[:2] / root[2:]
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"interrupted write")
    object_quarantine.copy_reachable_objects_to_bare(SimpleNamespace(store=store), bare.path, [root])
    bare.run("update-ref", "refs/tags/restored", root)
    bare.run("fsck", "--full", "--strict")


@pytest.mark.parametrize("corruption", ["truncated", "wrong_hash", "wrong_type"])
def test_corrupt_graph_prevents_gc_instead_of_becoming_legacy_leaf(component_repo, corruption):
    repo = component_repo.repo
    blob = repo.store.put_blob(b"kept")
    if corruption == "wrong_type":
        root = put(repo.store, "tag", tag(blob, "tree"))
    else:
        root = put(repo.store, "tag", tag(blob, "blob"))
        # Deliberately bypass content-addressed writes to simulate damage.
        physical = repo.store._backend._path_for(root)
        physical.write_bytes(b"truncated" if corruption == "truncated" else encode_object("blob", b"wrong")[1])
    orphan = repo.store.put_blob(b"unrelated candidate")
    repo.history.list_version_ref_roots = lambda: [root]
    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)
    assert result.sweep_skipped_for_safety
    assert result.deleted_count == 0
    assert repo.store.exists(blob) and repo.store.exists(orphan)


def test_materialization_rejects_tag_type_mismatch(tmp_path):
    store = ObjectStore(tmp_path / "canonical")
    blob = store.put_blob(b"not a tree")
    root = put(store, "tag", tag(blob, "tree"))
    bare = Git.init(tmp_path / "bad.git", bare=True)
    with pytest.raises(RuntimeError, match="type"):
        object_quarantine.copy_reachable_objects_to_bare(SimpleNamespace(store=store), bare.path, [root])
