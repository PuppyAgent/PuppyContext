"""Tree-closure integrity: writes must never publish a dangling tree.

A "Damaged folder" in the UI is the on-disk shape of a tree that references a
subtree object the store can't resolve. These tests pin the invariant that the
tree builders / grafts always persist a COMPLETE closure (so a write can't
produce that shape), and that ``find_missing_tree_objects`` detects the shape
when an object does go missing.
"""

from __future__ import annotations

import pytest

from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import decode_tree
from src.version_engine.write_engine.tree_objects import (
    build_tree_from_blob_ids,
    build_tree_from_files,
    find_missing_tree_objects,
)


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "objects")


NESTED = {
    "README.md": b"top",
    "docs/guide.md": b"guide",
    "docs/sub/deep.md": b"deep",
    "src/app.py": b"print(1)",
}


def _subtree_id(store: ObjectStore, root: str, name: str) -> str:
    return next(
        entry.sha1_hex for entry in decode_tree(store.get_object(root)[1]) if entry.name == name
    )


def test_build_tree_from_files_persists_complete_closure(store):
    root = build_tree_from_files(store, NESTED)
    assert find_missing_tree_objects(store, root) == []


def test_build_tree_from_blob_ids_persists_complete_closure(store):
    blob_ids = {path: store.put_blob(content) for path, content in NESTED.items()}
    root = build_tree_from_blob_ids(store, blob_ids)
    assert find_missing_tree_objects(store, root) == []


def test_find_missing_tree_objects_detects_dangling_subtree(store):
    root = build_tree_from_files(store, NESTED)
    docs_subtree = _subtree_id(store, root, "docs")

    # Simulate the corruption: the docs/ subtree object disappears from the
    # store while the root tree still references it (GC over-sweep / lost index).
    assert store._backend.delete(docs_subtree)

    missing = find_missing_tree_objects(store, root)
    assert docs_subtree in missing


def test_find_missing_tree_objects_detects_dangling_blob(store):
    root = build_tree_from_files(store, {"README.md": b"top"})
    blob_id = next(
        entry.sha1_hex
        for entry in decode_tree(store.get_object(root)[1])
        if entry.name == "README.md"
    )
    assert store._backend.delete(blob_id)
    assert blob_id in find_missing_tree_objects(store, root)


def test_find_missing_tree_objects_empty_for_empty_root(store):
    assert find_missing_tree_objects(store, "") == []
