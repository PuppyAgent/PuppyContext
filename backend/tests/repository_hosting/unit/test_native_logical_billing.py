"""Verified-graph logical bytes, not invoice settlement or HTTP admission proof."""
from dataclasses import replace

import pytest

from src.platform.billing.storage import logical_tree_bytes, logical_verified_tree_bytes
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    MODE_EXECUTABLE,
    MODE_FILE,
    MODE_GITLINK,
    MODE_SYMLINK,
    TreeEntry,
)
from src.version_engine.write_engine.tree import write_tree

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_verified_logical_usage_preserves_path_multiplicity_without_history(tmp_path, object_format):
    store = ObjectStore(tmp_path, object_format=object_format)
    blob, link, historical = (store.put_blob(value) for value in (b'content', b'../target', b'H' * 4096))
    shared = write_tree(store, [TreeEntry('file', MODE_FILE, blob), TreeEntry('executable', MODE_EXECUTABLE, blob),
                               TreeEntry('external-same-oid', MODE_GITLINK, blob)])
    root = write_tree(store, [TreeEntry('a', MODE_DIR, shared), TreeEntry('b', MODE_DIR, shared),
                             TreeEntry('direct', MODE_FILE, blob), TreeEntry('link', MODE_SYMLINK, link)])
    old = write_tree(store, [TreeEntry('historical', MODE_FILE, historical)])

    def commit(tree, parent=''):
        parents = f'parent {parent}\n' if parent else ''
        return store.put_commit((f'tree {tree}\n{parents}author Test <test@example.test> 0 +0000\n'
                                 'committer Test <test@example.test> 0 +0000\n\nTest\n').encode())

    head = commit(root, commit(old))
    manifest = ClosureVerifier(store._backend, object_format=object_format).verify({head: 'commit'})
    assert historical in manifest.objects
    measured = logical_verified_tree_bytes(manifest, root)
    assert measured == logical_tree_bytes(store, root) == 5 * len(b'content') + len(b'../target')
    assert manifest.total_bytes > measured + 4096  # Retained body bytes is a different metric.


@pytest.mark.parametrize('levels', [52, 63])
def test_shared_tree_dag_is_not_expanded_into_exponential_paths(tmp_path, levels):
    store = ObjectStore(tmp_path)
    blob = store.put_blob(b'x')
    tree = write_tree(store, [TreeEntry('file', MODE_FILE, blob)])
    for _ in range(levels):
        tree = write_tree(store, [TreeEntry('a', MODE_DIR, tree), TreeEntry('b', MODE_DIR, tree)])
    manifest = ClosureVerifier(store._backend).verify({tree: 'tree'})
    assert len(manifest.objects) == levels + 2
    if levels == 63:
        with pytest.raises(OverflowError, match='usage counter range'):
            logical_verified_tree_bytes(manifest, tree)
    else:
        assert logical_verified_tree_bytes(manifest, tree) == 2**levels


def test_logical_usage_requires_complete_typed_closure(tmp_path):
    store = ObjectStore(tmp_path)
    blob = store.put_blob(b'content')
    root = write_tree(store, [TreeEntry('file', MODE_FILE, blob)])
    manifest = ClosureVerifier(store._backend).verify({root: 'tree'})
    with pytest.raises(ValueError, match='verified tree'):
        logical_verified_tree_bytes(manifest, blob)
    incomplete = replace(manifest, objects={root: manifest.objects[root]})
    with pytest.raises(ValueError, match='complete typed tree closure'):
        logical_verified_tree_bytes(incomplete, root)
    cyclic = replace(manifest, objects={root: replace(manifest.objects[root], edges=((root, 'tree'),))})
    with pytest.raises(ValueError, match='logical tree cycle'):
        logical_verified_tree_bytes(cyclic, root)
