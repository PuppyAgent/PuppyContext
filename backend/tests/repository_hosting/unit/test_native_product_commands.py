"""Native Product normalization and authority-boundary checks (no service I/O)."""
import base64
from unittest.mock import Mock

import pytest

from src.platform.repository_target.models import ProjectRootTarget, ResolvedRepositoryView
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.adapters.product.native_commands import compile_native_command
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.native_operation_writer import NativeWriteBase

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("prefix,excludes", [("docs", ()), ("", ("private/**",))])
def test_native_product_cannot_construct_a_bounded_root_grant(prefix, excludes):
    target = ProjectRootTarget(project_id="project")
    # The canonical immutable view already rejects widening at construction;
    # the initial test incorrectly expected a later admission-stage failure.
    with pytest.raises(ValueError, match="Project root view must be unprefixed, unexcluded"):
        ResolvedRepositoryView(target, prefix, excludes, "rw")


@pytest.mark.parametrize("argument", [{"scope": "docs"}, {"policy": "replace"}, {"source_root_hash": "1"*40}])
def test_native_product_does_not_drop_unimplemented_producer_semantics(argument):
    with pytest.raises(ValueError, match="unsupported"):
        compile_native_command(VersionWriteCommandService(Mock()), "write",
                               {"path": "test", "content": "bytes", "node_type": "file", **argument})


def test_native_product_digest_binds_operation_flags_content_and_paths(tmp_path):
    commands = VersionWriteCommandService(Mock())
    original = {"path": "one", "content": "bytes", "node_type": "file"}
    digest, splice, response = compile_native_command(commands, "write", original)
    store = ObjectStore(tmp_path)
    empty = encode_object("tree", b"")[0]
    root, changes = splice(store, empty)
    assert response["path"] == "one" and changes == [("add", "one")]
    again, repeated, _ = compile_native_command(commands, "write", dict(original))
    assert again == digest and repeated(store, empty)[0] == root
    assert compile_native_command(commands, "write", {**original, "content": "other"})[0] != digest
    assert compile_native_command(commands, "write", {**original, "path": "two"})[0] != digest
    move = {"old_path": "one", "new_path": "two"}
    assert compile_native_command(commands, "move", move)[0] != compile_native_command(commands, "move", {**move, "no_clobber": True})[0]


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_native_product_preserves_modes_and_ordered_overwrite_audit(tmp_path, format):
    from src.version_engine.write_engine.git_object_format import (
        MODE_EXECUTABLE,
        MODE_SYMLINK,
        TreeEntry,
        encode_tree,
    )
    from src.version_engine.write_engine.tree import read_tree_entries
    store = ObjectStore(tmp_path, object_format=format)
    blob = store.put_blob(b"literal link bytes")
    root = store.put_tree(encode_tree([TreeEntry("one", MODE_SYMLINK, blob), TreeEntry("two", MODE_EXECUTABLE, blob)], object_format=format))
    commands = VersionWriteCommandService(Mock())
    _, splice, _ = compile_native_command(commands, "move", {"old_path": "one", "new_path": "two"})
    tree, changes = splice(store, root)
    assert changes == [("delete", "one"), ("delete", "two"), ("add", "two")]
    assert read_tree_entries(store, tree) == [TreeEntry("two", MODE_SYMLINK, blob)]
    _, reject, _ = compile_native_command(commands, "move", {"old_path": "one", "new_path": "two", "no_clobber": True})
    with pytest.raises(FileExistsError):
        reject(store, root)


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_native_product_byte_path_normalization_and_roundtrip(tmp_path, format):
    from src.version_engine.adapters.product.native_commands import apply_byte_paths, response_paths
    from src.version_engine.write_engine.tree import read_tree_entries
    raw = b"raw-\xff\\name"
    arguments = apply_byte_paths({"path": "", "content": "bytes", "node_type": "file"},
                                {"path": base64.b64encode(raw).decode()})
    commands = VersionWriteCommandService(Mock())
    _digest, splice, response = compile_native_command(commands, "write", arguments)
    store = ObjectStore(tmp_path, object_format=format)
    root, _changes = splice(store, encode_object("tree", b"", object_format=format)[0])
    assert read_tree_entries(store, root)[0].name.encode("utf-8", "surrogateescape") == raw
    wire = response_paths(response)
    assert base64.b64decode(wire["path_bytes_b64"]) == raw
    wire["path"].encode("utf-8")  # display is safe JSON, not byte identity


@pytest.mark.parametrize("raw", [b"", b"/absolute", b"a/../b", b"a//b", b"a/", b"bad\0"])
def test_native_product_byte_paths_reject_noncanonical_paths(raw):
    from src.version_engine.adapters.product.native_commands import apply_byte_paths
    with pytest.raises(ValueError, match="byte path"):
        apply_byte_paths({"path": ""}, {"path": base64.b64encode(raw).decode()})


def test_native_product_byte_paths_cannot_replace_a_second_identity():
    from src.version_engine.adapters.product.native_commands import apply_byte_paths
    with pytest.raises(ValueError, match="not both"):
        apply_byte_paths({"path": "existing"}, {"path": "bmV3"})
    for slot in ("files/-1/path", "files/01/path", "paths/0", "content", "native"):
        with pytest.raises(ValueError, match="slot"):
            apply_byte_paths({"files": [{"path": ""}]}, {slot: "bmV3"})


@pytest.mark.parametrize("result", [None, {}, {"project_id": "other", "generation": 1, "status": "committed"},
                                   {"project_id": "project", "generation": True, "status": "committed"}])
def test_native_product_rejects_incomplete_or_foreign_result_binding(result):
    from src.version_engine.write_engine.native_operation_writer import NativeOperationWriter
    with pytest.raises(RuntimeError, match="result binding"):
        NativeOperationWriter._result({"project_id": "project", "generation": 1, "proposal": {}, "result": result})


@pytest.mark.parametrize("change", [{"object_format": []}, {"generation": True}, {"expected_oid": 1},
                                  {"head_guard": {"kind": "symbolic", "target_b64": "cmVmcy9oZWFkcy90b3BpYw=="}},
                                  {"tree_oid": ""}, {"target_ref_b64": "YQ=="}])
def test_native_product_base_rejects_malformed_or_inconsistent_provenance(change):
    base = {"repository_profile": "native", "object_format": "sha1", "generation": 1,
            "target_ref_b64": base64.b64encode(b"refs/heads/main").decode(),
            "expected_oid": None, "tree_oid": encode_object("tree", b"")[0], "head_guard": None}
    with pytest.raises(ValueError):
        NativeWriteBase.parse({**base, **change})
