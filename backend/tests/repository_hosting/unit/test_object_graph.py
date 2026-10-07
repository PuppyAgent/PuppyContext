"""Byte/graph regressions against stock Git, not full hosting acceptance."""

from types import SimpleNamespace

import pytest

from src.version_engine.derived.object_gc import mark_reachable_objects
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
            (b"\xff", b"100644", blob),
            (b"\xc3\xa9", b"100644", blob),
            (b"\x80", b"120000", blob),
            (b"a", b"40000", empty),
            (b"a.c", b"100644", blob),
            (b"a0", b"100644", blob),
        ]
    ]
    native_input = b"".join(
        entry.mode
        + (b" tree " if entry.is_dir else b" blob ")
        + entry.sha1_hex.encode()
        + b"\t"
        + entry.name.encode("utf-8", "surrogateescape")
        + b"\0"
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
        (OID, "tree"),
        (parent, "commit"),
        (OID, "commit"),
    ]
    assert [edge.oid for edge in object_edges("commit", raw, follow_history=False)] == [OID]
    assert decode_commit(raw)["parents"] == [parent, OID]
    assert decode_commit(raw)["message"].encode("utf-8", "surrogateescape") == b"msg\xff"


@pytest.mark.parametrize("kind", ["blob", "tree", "commit", "tag"])
def test_tag_edges_preserve_target_type_and_non_utf8_message(kind):
    raw = tag(OID, kind, b"signed-\xff\n")
    assert [(edge.oid, edge.kind) for edge in object_edges("tag", raw)] == [(OID, kind)]
    assert decode_tag(raw)["object"] == OID


@pytest.mark.parametrize(
    "kind,body",
    [
        ("commit", b"parent " + OID.encode() + b"\n\nno tree\n"),
        ("commit", f"tree {OID}\ntree {OID}\n\n".encode()),
        ("commit", f"tree {OID}\nparent garbage\n\n".encode()),
        ("tag", f"object {OID}\ntype unknown\n\n".encode()),
        ("tag", f"object {OID}\nobject {OID}\ntype blob\n\n".encode()),
        ("tag", b"object " + b"0" * 40 + b"\ntype blob\n\n"),
        ("tree", b"999999 file\0" + bytes.fromhex(OID)),
        ("tree", b"100644 file\0" + bytes.fromhex(OID)[:-1]),
        ("unknown", b"anything"),
    ],
)
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
