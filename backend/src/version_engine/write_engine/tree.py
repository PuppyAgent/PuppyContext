"""Git tree operations owned by PuppyOne."""

from __future__ import annotations

from pathlib import Path

from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    MODE_FILE,
    TreeEntry,
    decode_tree,
    encode_tree,
)
from src.version_engine.storage.object_store import ObjectStore

MAX_DEPTH = 100


def write_blob(store: ObjectStore, content: bytes) -> str:
    return store.put_blob(content)


def write_tree(store: ObjectStore, entries) -> str:
    if isinstance(entries, dict):
        converted: list[TreeEntry] = []
        for name, value in entries.items():
            if len(value) not in {2, 3}:
                raise ValueError("tree entry requires kind, OID and optional mode")
            typ, sha1 = value[:2]
            mode = value[2] if len(value) == 3 else (MODE_DIR if typ == "T" else MODE_FILE)
            converted.append(
                TreeEntry(
                    name=name,
                    mode=mode,
                    sha1_hex=sha1,
                )
            )
        entries = converted
    return store.put_tree(encode_tree(entries, object_format=store.object_format))


def read_tree(store: ObjectStore, h: str) -> dict:
    obj_type, content = store.get_object(h)
    if obj_type != "tree":
        raise ValueError(f"object {h} is a {obj_type}, expected tree")
    out: dict = {}
    for entry in decode_tree(content, object_format=store.object_format):
        out[entry.name] = ["T" if entry.is_dir else "B", entry.sha1_hex]
    return out


def read_tree_entries(store: ObjectStore, h: str) -> list[TreeEntry]:
    obj_type, content = store.get_object(h)
    if obj_type != "tree":
        raise ValueError(f"object {h} is a {obj_type}, expected tree")
    return decode_tree(content, object_format=store.object_format)


def scan_dir(store: ObjectStore, dirpath: Path, ignore, _depth: int = 0) -> str:
    if _depth > MAX_DEPTH:
        raise RecursionError(f"directory nesting exceeds {MAX_DEPTH} levels")
    entries: list[TreeEntry] = []
    for child in sorted(dirpath.iterdir()):
        if ignore.should_ignore(child.name):
            continue
        if child.is_file():
            entries.append(
                TreeEntry(name=child.name, mode=MODE_FILE, sha1_hex=write_blob(store, child.read_bytes()))
            )
        elif child.is_dir():
            entries.append(
                TreeEntry(name=child.name, mode=MODE_DIR, sha1_hex=scan_dir(store, child, ignore, _depth + 1))
            )
    return store.put_tree(encode_tree(entries, object_format=store.object_format))


def tree_to_flat(
    store: ObjectStore, tree_hash: str, prefix: str = "", *, include_gitlinks: bool = True,
) -> dict:
    result: dict = {}
    stack = [(tree_hash, prefix)]
    while stack:
        th, pfx = stack.pop()
        for entry in read_tree_entries(store, th):
            path = f"{pfx}{entry.name}" if not pfx else f"{pfx}/{entry.name}"
            if entry.is_dir:
                stack.append((entry.sha1_hex, path))
            elif include_gitlinks or not entry.is_gitlink:
                result[path] = entry.sha1_hex
    return result


def tree_path_modes(store: ObjectStore, tree_hash: str, prefix: str = "") -> dict:
    """Return ``{path: mode_bytes}`` for every FILE in a tree.

    Companion to ``tree_to_flat`` so the flatten→filter→rebuild paths can
    preserve the original Git blob mode (executable ``100755``, symlink
    ``120000``, gitlink ``160000``) instead of downgrading every entry to
    ``100644``. Only non-directory entries are returned.
    """
    result: dict = {}
    if not tree_hash:
        return result
    stack = [(tree_hash, prefix)]
    while stack:
        th, pfx = stack.pop()
        for entry in read_tree_entries(store, th):
            path = f"{pfx}{entry.name}" if not pfx else f"{pfx}/{entry.name}"
            if entry.is_dir:
                stack.append((entry.sha1_hex, path))
            else:
                result[path] = entry.mode
    return result


def collect_reachable_hashes(store: ObjectStore, tree_hash: str) -> set:
    result: set = set()
    visited_trees: set[str] = set()
    stack = [tree_hash]
    while stack:
        th = stack.pop()
        if th in visited_trees:
            continue
        visited_trees.add(th)
        result.add(th)
        for entry in read_tree_entries(store, th):
            if entry.is_gitlink:
                continue  # External repository identity, not a local object.
            result.add(entry.sha1_hex)
            if entry.is_dir and entry.sha1_hex not in visited_trees:
                stack.append(entry.sha1_hex)
    return result
