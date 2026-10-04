"""Product-side object/tree primitives must retain the repository's format.

No transport repository is created by the product code under test. Stock Git
is used only as an independent format oracle, before the pure-code checks.
"""
from __future__ import annotations

import subprocess

import pytest

from src.version_engine.adapters.product.tree_patch import (
    splice_copy,
    splice_move,
    splice_multi_put_refs,
    splice_put_blob,
    splice_put_blob_ref,
    splice_remove,
)
from src.version_engine.derived.projection import graft_subtree
from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    MODE_EXECUTABLE,
    MODE_FILE,
    MODE_GITLINK,
    MODE_SYMLINK,
    TreeEntry,
    encode_object,
    encode_tree,
)
from src.version_engine.write_engine.tree import (
    collect_reachable_hashes,
    read_tree_entries,
    tree_path_modes,
    tree_to_flat,
    write_tree,
)
from src.version_engine.write_engine.tree_objects import (
    build_tree_from_blob_ids,
    find_missing_tree_objects,
)
from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_product_store_matches_stock_git_and_survives_cold_reads(tmp_path, monkeypatch, object_format):
    oracle = Git.init(tmp_path / "oracle.git", bare=True, format=object_format)
    content = b"product save\x00\xff\n"
    expected = oracle.run("hash-object", "--stdin", input=content).stdout.decode().strip()

    def no_transport(*args, **kwargs):
        raise AssertionError("product object operations must not invoke Git")

    monkeypatch.setattr(subprocess, "run", no_transport)
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    assert store.object_format == object_format
    assert store.put_blob(content) == expected
    cold = ObjectStore(tmp_path / "objects", object_format=object_format)
    assert cold.get_object(expected) == ("blob", content)
    assert cold.get_objects_many([expected]) == {expected: ("blob", content)}
    assert cold.get_loose_many([expected]) == {expected: encode_object("blob", content, object_format=object_format)[1]}


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_product_empty_tree_is_intrinsic_in_its_declared_format(tmp_path, object_format):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    oid, loose = encode_object("tree", b"", object_format=object_format)
    assert store.put_tree(b"") == oid
    assert store.exists(oid)
    assert store.exists_many([oid]) == {oid}
    assert store.get_object(oid) == ("tree", b"")
    assert store.get_loose(oid) == loose
    assert store.get_loose_many([oid]) == {oid: loose}
    assert store.get_objects_many([oid]) == {oid: ("tree", b"")}
    assert store.all_hashes() == []


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_product_tree_preserves_byte_names_and_modes(tmp_path, object_format):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    blob = store.put_blob(b"data")
    external = "f" * len(blob)
    entries = [
        TreeEntry("opaque-\udcff", MODE_FILE, blob),
        TreeEntry("executable", MODE_EXECUTABLE, blob),
        TreeEntry("link", MODE_SYMLINK, blob),
        TreeEntry("submodule", MODE_GITLINK, external),
    ]
    raw = encode_tree(entries, object_format=object_format)
    root = write_tree(store, entries)
    assert store.get_object(root) == ("tree", raw)
    assert sorted(read_tree_entries(store, root)) == sorted(entries)
    assert not store.exists(external)
    assert collect_reachable_hashes(store, root) == {root, blob}


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_declared_store_format_cannot_accept_another_formats_identity(tmp_path, object_format):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    other = "sha256" if object_format == "sha1" else "sha1"
    foreign, loose = encode_object("blob", b"same framing, different identity", object_format=other)
    store._backend.put(foreign, loose)
    with pytest.raises(ObjectNotFoundError, match="object corrupt"):
        store.get_object(foreign)
    with pytest.raises(ObjectNotFoundError, match="object corrupt"):
        store.get_objects_many([foreign])


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("operation", ["put", "put_ref", "multi_put", "remove", "move", "copy"])
def test_product_splices_preserve_unmodified_native_entries(tmp_path, object_format, operation):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    blob = store.put_blob(b"before")
    incoming = store.put_blob(b"after")
    external = "f" * len(blob)
    files = {
        "opaque-\udcff": blob, "executable": blob, "link": blob, "external": external,
        "nested/executable": blob, "nested/link": blob,
        "nested/external": external, "nested/edit": blob,
    }
    modes = {path: MODE_FILE for path in files}
    modes.update({"executable": MODE_EXECUTABLE, "link": MODE_SYMLINK, "external": MODE_GITLINK,
                  "nested/executable": MODE_EXECUTABLE, "nested/link": MODE_SYMLINK,
                  "nested/external": MODE_GITLINK})
    root = build_tree_from_blob_ids(store, files, modes=modes)
    assert find_missing_tree_objects(store, root) == []
    if operation == "put":
        new, _ = splice_put_blob(store, root, "nested/edit", b"after")
        files["nested/edit"] = incoming
    elif operation == "put_ref":
        new, _ = splice_put_blob_ref(store, root, "nested/edit", incoming)
        files["nested/edit"] = incoming
    elif operation == "multi_put":
        new, _ = splice_multi_put_refs(store, root, [("nested/edit", incoming)])
        files["nested/edit"] = incoming
    elif operation == "remove":
        new, _ = splice_remove(store, root, ["nested/edit"])
        del files["nested/edit"]
        del modes["nested/edit"]
    elif operation == "move":
        new, _ = splice_move(store, root, "nested/executable", "moved")
        files["moved"] = files.pop("nested/executable")
        modes["moved"] = modes.pop("nested/executable")
    else:
        new, _ = splice_copy(store, root, "nested/executable", "copied")
        files["copied"] = blob
        modes["copied"] = MODE_EXECUTABLE
    assert tree_to_flat(store, new) == files
    assert tree_path_modes(store, new) == modes
    assert find_missing_tree_objects(store, new) == []
    assert not store.exists(external)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_copy_same_blob_replaces_destination_mode(tmp_path, object_format):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    blob = store.put_blob(b"same bytes")
    root = write_tree(store, [TreeEntry("source", MODE_EXECUTABLE, blob), TreeEntry("dest", MODE_FILE, blob)])
    new, changes = splice_copy(store, root, "source", "dest")
    assert new != root
    # Preserve the existing copy-overwrite audit contract (delete, then add).
    assert changes == [("delete", "dest"), ("add", "dest")]
    assert tree_path_modes(store, new) == {"source": MODE_EXECUTABLE, "dest": MODE_EXECUTABLE}


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("operation", ["put", "put_ref", "multi_put"])
def test_blob_put_preserves_blob_mode_but_replaces_a_gitlink(tmp_path, object_format, operation):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    blob = store.put_blob(b"content")
    # Gitlink identity is external: it may coincidentally name a local blob.
    root = write_tree(store, [TreeEntry("exec", MODE_EXECUTABLE, blob), TreeEntry("link", MODE_GITLINK, blob)])

    def put(base, path):
        if operation == "put":
            return splice_put_blob(store, base, path, b"content")
        if operation == "put_ref":
            return splice_put_blob_ref(store, base, path, blob)
        return splice_multi_put_refs(store, base, [(path, blob)])

    assert put(root, "exec") == (root, [])
    new, changes = put(root, "link")
    assert new != root
    assert changes == [("update", "link")]
    assert tree_path_modes(store, new) == {"exec": MODE_EXECUTABLE, "link": MODE_FILE}


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_scope_graft_preserves_other_native_entries(tmp_path, object_format):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    blob = store.put_blob(b"scope content")
    before = write_tree(store, [TreeEntry("before", MODE_FILE, blob)])
    after = write_tree(store, [TreeEntry("after", MODE_FILE, blob)])
    unrelated = [TreeEntry("opaque-\udcff", MODE_EXECUTABLE, blob),
                 TreeEntry("external", MODE_GITLINK, "f" * len(blob))]
    root = write_tree(store, [*unrelated, TreeEntry("docs", MODE_DIR, before)])
    grafted = graft_subtree(store, root, "docs", after)
    assert grafted == write_tree(store, [*unrelated, TreeEntry("docs", MODE_DIR, after)])
    assert find_missing_tree_objects(store, grafted) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_async_product_store_keeps_declared_format(tmp_path, object_format):
    store = ObjectStore(tmp_path / "objects", object_format=object_format)
    oid, loose = encode_object("blob", b"async save", object_format=object_format)
    assert await store.async_put(b"async save") == oid
    assert await store.async_get(oid) == b"async save"
    assert await store.async_get_loose(oid) == loose
    empty, empty_loose = encode_object("tree", b"", object_format=object_format)
    await store.async_put_loose(empty, empty_loose)
    assert await store.async_exists(empty)
    assert await store.async_get_loose(empty) == empty_loose
    assert set(store.all_hashes()) == {oid}


def test_unsupported_store_format_fails_before_storage_io(tmp_path):
    with pytest.raises(ValueError, match="unsupported Git object format"):
        ObjectStore(tmp_path / "objects", object_format="md5")
    assert not (tmp_path / "objects").exists()
