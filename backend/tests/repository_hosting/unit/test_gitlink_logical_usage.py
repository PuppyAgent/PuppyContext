"""Logical active storage bills blob paths, not external submodule commits."""
from __future__ import annotations

import pytest

from src.platform.billing.storage import (
    logical_tree_bytes,
    logical_tree_delta,
    oversized_new_logical_file,
)
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import (
    MODE_FILE,
    MODE_GITLINK,
    MODE_SYMLINK,
    TreeEntry,
)
from src.version_engine.write_engine.tree import write_tree

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_logical_storage_excludes_gitlinks_but_counts_each_blob_path(tmp_path, object_format):
    store = ObjectStore(tmp_path, object_format=object_format)
    blob = store.put_blob(b"data")
    root = write_tree(store, [
        TreeEntry("first", MODE_FILE, blob), TreeEntry("copy", MODE_FILE, blob),
        TreeEntry("symlink", MODE_SYMLINK, blob),
        TreeEntry("external", MODE_GITLINK, "f" * len(blob)),
        TreeEntry("coincidental-local-oid", MODE_GITLINK, blob),
    ])
    assert logical_tree_bytes(store, root) == 12
    assert logical_tree_delta(store, "", root) == 12
    assert logical_tree_delta(store, root, "") == -12


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_replacing_gitlink_with_blob_is_a_new_billable_file(tmp_path, object_format):
    store = ObjectStore(tmp_path, object_format=object_format)
    blob = store.put_blob(b"oversized")
    old = write_tree(store, [TreeEntry("path", MODE_GITLINK, blob)])
    new = write_tree(store, [TreeEntry("path", MODE_FILE, blob)])
    assert logical_tree_delta(store, old, new) == 9
    assert oversized_new_logical_file(store, old, new, 8) == ("path", 9)
    assert oversized_new_logical_file(store, new, old, 8) is None
