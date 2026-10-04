"""Listing damage is not permission to replace an acknowledged Project root."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.domain.errors import VersionReadError
from src.version_engine.read.tree_reader import VersionTreeReader
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.tree import read_tree
from src.version_engine.write_engine.tree_objects import build_tree_from_files
from tests.version_engine.test_server_repo import FakeHistoryManager

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize('damage', ['missing_tree', 'missing_blob'])
def test_listing_damaged_content_preserves_acknowledged_root_and_healthy_siblings(tmp_path, damage):
    store = ObjectStore(tmp_path / 'objects')
    root = build_tree_from_files(store, {'outside.md': b'acknowledged outside scope',
                                        'docs/good.md': b'acknowledged scoped file',
                                        'broken/lost.md': b'acknowledged unavailable file'})
    entries = read_tree(store, root)
    history = FakeHistoryManager()
    history.set_root_hash(root)
    history.set_scope_hash('docs', entries['docs'][1])
    missing = entries['broken'][1] if damage == 'missing_tree' else entries['outside.md'][1]
    loose = store._backend.get(missing)
    store._backend.delete(missing)
    cas = Mock(side_effect=history.cas_update_root_hash)
    repo = SimpleNamespace(store=store, history=history, get_all_scope_hashes=history.get_all_scope_hashes,
                           cas_update_root_hash=cas)
    repos = SimpleNamespace(get_repo=lambda _: repo, get_server_repo=lambda _: repo)
    reader = VersionTreeReader(repos)
    shown = reader.list_dir('p')
    assert {entry.name for entry in shown} == {'outside.md', 'docs', 'broken'}
    assert history.get_root_hash() == root
    cas.assert_not_called()
    assert reader.read_file('p', 'docs/good.md') == b'acknowledged scoped file'
    assert reader.get_root_hash('p') == root
    store._backend.put(missing, loose)
    assert reader.read_file('p', 'outside.md') == b'acknowledged outside scope'
    assert reader.read_file('p', 'broken/lost.md') == b'acknowledged unavailable file'
    assert history.get_root_hash() == root
    cas.assert_not_called()


@pytest.mark.parametrize('failure', ['metadata unavailable', 'native repository requires authority-aware access'])
@pytest.mark.parametrize('method,args', [('read_file', ('p', 'file')), ('read_file_range', ('p', 'file')),
                                        ('stat', ('p', 'file')), ('get_root_hash', ('p',)),
                                        ('get_head_commit_id', ('p',)),
                                        ('read_file_in_scope', ('p', 'docs', 'file')),
                                        ('read_file_range_in_scope', ('p', 'docs', 'file')),
                                        ('stat_in_scope', ('p', 'docs', 'file'))])
def test_reader_authority_failure_is_not_absence(failure, method, args):
    repos = SimpleNamespace(get_repo=Mock(side_effect=RuntimeError(failure)))
    reader = VersionTreeReader(repos)
    with pytest.raises(VersionReadError):
        getattr(reader, method)(*args)
