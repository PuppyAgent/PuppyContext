"""Native history keeps Git modes/raw names and nested initial file events."""

import json

import pytest

from src.version_engine.read.native_history import NativeHistory
from src.version_engine.write_engine.git_object_format import TreeEntry, encode_object, encode_tree
from src.version_engine.write_engine.native_tree_diff import native_tree_diff
from tests.repository_hosting.integration.test_ref_transaction_service import grant
from tests.repository_hosting.unit.test_native_product_reads import native_reads as native_fixture

native_reads = native_fixture

pytestmark = pytest.mark.hosting_component


def test_history_bytes_and_nested_creation(native_reads):
    ops, _manager, _wire, _calls, _objects, _root, commit, _blob, _child = native_reads
    with ops.open_read("p", grant("p")) as reader:
        history = NativeHistory(reader.snapshot)
        paths = {change["path"] for change in history.changes(commit)}
        assert "dir/inside" in paths
        assert b"raw-\xff".decode("utf-8", "surrogateescape") in paths
        assert history.timestamps(["dir/inside"])["dir/inside"]["created_at"]
        assert len(history.linear(10, path="dir/inside")) == 1
        json.dumps(history.nodes[commit]).encode("utf-8")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_mode_only_changes_are_visible(object_format):
    from types import SimpleNamespace

    objects = {}

    def put(mode):
        body = encode_tree(
            [TreeEntry("exec", mode, "a" * (40 if object_format == "sha1" else 64))],
            object_format=object_format,
        )
        oid = encode_object("tree", body, object_format=object_format)[0]
        objects[oid] = ("tree", body)
        return oid

    before, after = put(b"100644"), put(b"100755")
    assert native_tree_diff(
        SimpleNamespace(object_format=object_format, get_object=objects.__getitem__), before, after
    ) == [{"path": "exec", "op": "modified"}]
