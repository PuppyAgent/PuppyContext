"""Native read failures cannot replace acknowledged ref/tree facts."""

import copy

import pytest

from tests.repository_hosting.integration.test_ref_transaction_service import grant
from tests.repository_hosting.unit.test_native_product_reads import native_reads as native_fixture

native_reads = native_fixture

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("damage", ["tree", "blob"])
def test_missing_objects_do_not_rewrite_authority(native_reads, damage):
    ops, manager, wire, _calls, objects, root, commit, blob, child = native_reads
    missing = child if damage == "tree" else blob
    raw = objects.pop(missing)
    before = copy.deepcopy(wire)
    bound = ops.for_grant(grant("p"))
    with pytest.raises((KeyError, FileNotFoundError)):
        bound.read_file("p", "dir/inside")
    assert wire == before
    objects[missing] = raw
    assert bound.read_file("p", "dir/inside") == b"raw\x00bytes"
    assert bound.get_root_hash("p") == root
    assert bound.get_head_commit_id("p") == commit
    manager.get_repo.assert_not_called()
    manager.get_server_repo.assert_not_called()
