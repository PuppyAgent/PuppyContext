"""A1-1: PuppyOne's Git tree layer must round-trip non-regular blob modes
(executable 100755, symlink 120000, gitlink 160000) instead of rejecting
them (graft crash) or silently downgrading them to 100644 (merge/rebuild).
"""

from __future__ import annotations

import pytest

from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import (
    MODE_EXECUTABLE,
    MODE_FILE,
    MODE_GITLINK,
    MODE_SYMLINK,
    TreeEntry,
    decode_tree,
    encode_tree,
)
from src.version_engine.write_engine.tree import tree_path_modes
from src.version_engine.write_engine.tree_objects import (
    build_tree_from_blob_ids,
    build_tree_from_files,
)


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "objects")


# ── encode/decode round-trip ────────────────────────────────────────


def test_encode_tree_accepts_all_blob_modes():
    entries = [
        TreeEntry("run.sh", MODE_EXECUTABLE, "a" * 40),
        TreeEntry("link", MODE_SYMLINK, "b" * 40),
        TreeEntry("sub", MODE_GITLINK, "c" * 40),
        TreeEntry("doc.md", MODE_FILE, "d" * 40),
    ]
    body = encode_tree(entries)  # must NOT raise
    out = {e.name: e.mode for e in decode_tree(body)}
    assert out == {
        "run.sh": MODE_EXECUTABLE,
        "link": MODE_SYMLINK,
        "sub": MODE_GITLINK,
        "doc.md": MODE_FILE,
    }


def test_encode_tree_still_rejects_garbage_mode():
    with pytest.raises(ValueError, match="unsupported git tree mode"):
        encode_tree([TreeEntry("x", b"100777", "a" * 40)])


# ── builders preserve modes ─────────────────────────────────────────


def test_build_tree_from_files_preserves_modes(store):
    files = {"run.sh": b"#!/bin/sh\n", "link": b"target/path", "doc.md": b"hi\n"}
    modes = {"run.sh": MODE_EXECUTABLE, "link": MODE_SYMLINK}
    tree = build_tree_from_files(store, files, modes=modes)
    got = tree_path_modes(store, tree)
    assert got["run.sh"] == MODE_EXECUTABLE
    assert got["link"] == MODE_SYMLINK
    assert got["doc.md"] == MODE_FILE  # default when absent


def test_build_tree_from_files_defaults_when_no_modes(store):
    tree = build_tree_from_files(store, {"a.txt": b"x"})
    assert tree_path_modes(store, tree)["a.txt"] == MODE_FILE


def test_build_tree_from_blob_ids_preserves_modes(store):
    sh = store.put_blob(b"#!/bin/sh\n")
    md = store.put_blob(b"hi\n")
    tree = build_tree_from_blob_ids(
        store,
        {"bin/run.sh": sh, "doc.md": md},
        modes={"bin/run.sh": MODE_EXECUTABLE},
    )
    got = tree_path_modes(store, tree)
    assert got["bin/run.sh"] == MODE_EXECUTABLE
    assert got["doc.md"] == MODE_FILE


# ── graft no longer crashes on a non-regular sibling ────────────────
