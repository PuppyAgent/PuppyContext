"""tree_splice — direct Git-tree mutations for typed write operations.

Each function transforms a scope's tree:

    (store, root_hash, ...) -> (new_root_hash, changes)

``changes`` is a list of ``("add"|"update"|"delete", rel_path)`` tuples in
the same order the version history schema expects. The caller is responsible
for prefixing ``rel_path`` with the scope path before persisting.

Cost: ``O(D)`` per mutation where ``D`` is the depth of the affected path
(typically < 10), independent of total scope size. This keeps product writes
on the same shallow Git-tree update model as scoped Git submissions instead
of flattening the whole repository for every operation.

Move / rename operations do **not** download blob contents. They
relocate tree-node hashes only. Folder rename of a 1000-file subtree
costs the same as a 1-file rename.

Idempotency: when the mutation produces the same root hash as the input
(e.g. writing identical content, deleting a missing path), ``changes`` is
empty. Callers should detect this and skip CAS / history / audit — that's
how the editor's repeat-save case avoids polluting history.

Concurrency: these functions are pure with respect to the underlying
``ObjectStore`` (writes are content-addressed and idempotent). They can
be retried safely inside a CAS loop.
"""

from __future__ import annotations

from typing import Iterable

from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.write_engine import tree as tree_mod
from src.version_engine.storage.object_store import ObjectStore

# Each change is ``(action, rel_path)``. Action is one of "add" / "update"
# / "delete", matching version history conventions.
Change = tuple[str, str]

# A batched mutation entry. The first slot is the discriminator:
#   ("put",     rel_path, content)    – upsert a blob from raw bytes
#   ("put_ref", rel_path, blob_hash)  – upsert pointing at an already-staged blob
#   ("rm",      rel_path)             – remove a file or subtree
#   ("mv",      old_rel, new_rel)     – move / rename without blob downloads
BatchOp = tuple


# ── Internal helpers ──────────────────────────────────


def _split_path(path: str) -> list[str]:
    return [seg for seg in path.strip("/").split("/") if seg]


def _read_tree_or_empty(store: ObjectStore, h: str) -> dict:
    """Read a tree node, or return ``{}`` for empty/blank ``h``.

    Empty input means "no tree exists at this position yet" (e.g. a brand-new
    sub-scope or a missing intermediate directory mid-walk). Treating it as
    ``{}`` lets the splice algorithms create intermediate directories with
    ``mkdir -p`` semantics.
    """
    if not h:
        return {}
    return tree_mod.read_tree(store, h)


def _walk_spine(
    store: ObjectStore,
    root_hash: str,
    parts: list[str],
) -> tuple[list[dict], list[list | None]]:
    """Walk into the tree along ``parts`` as a folder spine.

    Returns ``(spine_entries, found)``:

    - ``spine_entries[i]`` is the entries-dict at depth ``i`` (root = 0).
      Always has length ``len(parts)``. Missing intermediate directories
      are represented as ``{}``.
    - ``found[i]`` is the existing ``["B"|"T", hash]`` entry for
      ``parts[i]`` in ``spine_entries[i]``, or ``None`` if missing.

    Each component in ``parts`` is required to be either missing or a
    folder. Encountering a file mid-walk is a structural error
    (``ValueError``) — the caller is asking to descend through something
    that isn't a directory. Callers that legitimately want to inspect a
    leaf-as-file should pass ``parts[:-1]`` and check the last name
    against the leaf entries directly.
    """
    spine: list[dict] = []
    found: list[list | None] = []
    cur = root_hash
    for i, name in enumerate(parts):
        entries = _read_tree_or_empty(store, cur)
        spine.append(entries)
        existing = entries.get(name)
        found.append(existing)
        if existing is None:
            cur = ""
            continue
        typ, h = existing
        if typ != "T":
            full = "/".join(parts[: i + 1])
            raise ValueError(
                f"path component {full!r} is a file, cannot descend into it",
            )
        cur = h
    return spine, found


def _rebuild_spine(
    store: ObjectStore,
    spine: list[dict],
    parts: list[str],
    leaf_entries: dict,
) -> str:
    """Rebuild the tree spine bottom-up after a leaf-level mutation.

    ``spine[i]`` is the entries-dict at depth ``i`` from ``_walk_spine``;
    we copy before mutating so the caller's spine isn't aliased. ``parts``
    is the spine path (NOT including the leaf name). ``leaf_entries`` is
    the post-mutation entries-dict at depth ``len(parts)`` — the *parent*
    directory of the leaf, already containing the desired final state.

    Empty intermediate directories are pruned: if removing the leaf's
    parent leaves a higher-up dir empty, we drop the empty dir from its
    parent rather than storing an empty tree as a child. Matches what
    users expect from ``rm -r`` followed by a directory listing — empty
    dirs evaporate.
    """
    cur_hash: str | None
    if leaf_entries:
        cur_hash = tree_mod.write_tree(store, leaf_entries)
    else:
        cur_hash = None  # signals "drop this entry from parent"

    for i in range(len(parts) - 1, -1, -1):
        parent_entries = dict(spine[i])
        name = parts[i]
        if cur_hash is None:
            parent_entries.pop(name, None)
        else:
            parent_entries[name] = ["T", cur_hash]
        if parent_entries:
            cur_hash = tree_mod.write_tree(store, parent_entries)
        else:
            cur_hash = None

    if cur_hash is None:
        # Whole tree collapsed to empty — return the canonical empty tree
        # rather than the "no tree" sentinel so downstream callers always
        # have a real hash to write to the authoritative scope-state row.
        return tree_mod.write_tree(store, {})
    return cur_hash


def _resolve_leaf_parent_hash(
    root_hash: str,
    spine_path: list[str],
    found: list[list | None],
) -> str:
    """Hash of the directory that holds the leaf for a put/check operation.

    With no spine (single-segment path), the leaf's parent is the root
    itself. Otherwise, it's the tree pointed to by the last walked
    component. Empty string means a parent intermediate dir is missing
    and will need to be created.
    """
    if not spine_path:
        return root_hash
    last = found[-1] if found else None
    return "" if last is None else last[1]


def _collect_affected_paths(
    store: ObjectStore,
    entry: list,
    base_path: str,
) -> list[str]:
    """Enumerate every path under a tree entry, for the changes list.

    Used during folder delete / move so the audit trail mentions each
    impacted file rather than just the folder root. Walks the subtree
    via ``tree_to_flat`` (tree-node reads only — no blob downloads).
    """
    typ, h = entry
    if typ == "B":
        return [base_path] if base_path else []
    try:
        flat = tree_mod.tree_to_flat(store, h)
    except ObjectNotFoundError:
        # A legacy/corrupt folder can still be unlinked safely from its parent
        # tree. Do not make delete depend on reading the missing subtree; record
        # the folder path itself as the versioned change so users can repair
        # historical bad pointers through an ordinary delete commit.
        return [base_path] if base_path else []
    if not flat:
        return [f"{base_path}/.keep"] if base_path else [".keep"]
    return [
        f"{base_path}/{p}" if base_path else p
        for p in flat.keys()
    ]


# ── Public splice operations ──────────────────────────


def splice_put_blob(
    store: ObjectStore,
    root_hash: str,
    rel_path: str,
    content: bytes,
) -> tuple[str, list[Change]]:
    """Replace or create a blob at ``rel_path``.

    Creates intermediate directories as needed (``mkdir -p`` semantics).

    Idempotent: writing identical content at the same path yields the
    same root hash and an empty changes list — caller should skip the
    commit.
    """
    parts = _split_path(rel_path)
    if not parts:
        raise ValueError("cannot put blob at empty path")

    new_blob_hash = tree_mod.write_blob(store, content)

    spine_path = parts[:-1]
    spine, found = _walk_spine(store, root_hash, spine_path)

    leaf_parent_hash = _resolve_leaf_parent_hash(root_hash, spine_path, found)
    leaf_entries = dict(_read_tree_or_empty(store, leaf_parent_hash))

    leaf_name = parts[-1]
    existing = leaf_entries.get(leaf_name)
    if existing is not None and existing[0] == "B" and existing[1] == new_blob_hash:
        return root_hash, []
    if existing is not None and existing[0] == "T":
        raise ValueError(
            f"cannot write file at {rel_path!r}: "
            f"path is currently a directory",
        )

    action = "add" if existing is None else "update"
    leaf_entries[leaf_name] = ["B", new_blob_hash]

    if not spine_path:
        new_root = tree_mod.write_tree(store, leaf_entries)
    else:
        new_root = _rebuild_spine(store, spine, spine_path, leaf_entries)

    if new_root == root_hash:
        return root_hash, []
    return new_root, [(action, rel_path.strip("/"))]


def splice_put_blob_ref(
    store: ObjectStore,
    root_hash: str,
    rel_path: str,
    blob_hash: str,
) -> tuple[str, list[Change]]:
    """Reference an existing blob at ``rel_path`` (no content write).

    Counterpart to :func:`splice_put_blob`: instead of taking raw bytes
    and calling ``tree_mod.write_blob``, this trusts the caller to have
    already staged the blob in ``store`` and points the tree at the
    supplied hash directly.

    **Caller contract**: ``blob_hash`` MUST already be present in
    ``store`` for the project. If it's missing, the resulting commit
    will reference a hash whose bytes don't exist; readers will
    fail-loud with ``ObjectNotFoundError`` (the read path verifies
    ``hash_bytes(get(h)) == h``) — there is no silent corruption, but
    the file becomes unreadable until the missing object is uploaded.

    Use this in pipelines where the bytes were materialized OUTSIDE
    the Python process (e.g. browser → S3 multipart upload, then
    server-side ``CopyObject`` into the version object key). Skipping
    ``write_blob`` saves a full round-trip through the process — for
    a 100 MB file that's 100 MB you don't have to pay for in
    backend RAM or hash time.

    Idempotent: pointing at the same hash that's already at this path
    yields the same root and an empty changes list, same as
    :func:`splice_put_blob`.
    """
    parts = _split_path(rel_path)
    if not parts:
        raise ValueError("cannot put blob ref at empty path")
    if not blob_hash:
        raise ValueError("blob_hash is required for splice_put_blob_ref")

    spine_path = parts[:-1]
    spine, found = _walk_spine(store, root_hash, spine_path)

    leaf_parent_hash = _resolve_leaf_parent_hash(root_hash, spine_path, found)
    leaf_entries = dict(_read_tree_or_empty(store, leaf_parent_hash))

    leaf_name = parts[-1]
    existing = leaf_entries.get(leaf_name)
    if existing is not None and existing[0] == "B" and existing[1] == blob_hash:
        return root_hash, []
    if existing is not None and existing[0] == "T":
        raise ValueError(
            f"cannot write file at {rel_path!r}: "
            f"path is currently a directory",
        )

    action = "add" if existing is None else "update"
    leaf_entries[leaf_name] = ["B", blob_hash]

    if not spine_path:
        new_root = tree_mod.write_tree(store, leaf_entries)
    else:
        new_root = _rebuild_spine(store, spine, spine_path, leaf_entries)

    if new_root == root_hash:
        return root_hash, []
    return new_root, [(action, rel_path.strip("/"))]


def splice_remove(
    store: ObjectStore,
    root_hash: str,
    rel_paths: Iterable[str],
) -> tuple[str, list[Change]]:
    """Remove one or more files or subtrees.

    Each path may be a file or a folder. Removing a folder drops its
    entire subtree (no blob downloads — we just unlink the tree
    pointer). Empty intermediate directories are pruned by
    ``_rebuild_spine``.

    Idempotent: paths that don't exist are silently skipped. If no paths
    actually existed, the input root is returned unchanged.
    """
    cur_root = root_hash
    all_changes: list[Change] = []

    for raw in rel_paths:
        parts = _split_path(raw)
        if not parts:
            continue

        spine_path = parts[:-1]
        spine, found = _walk_spine(store, cur_root, spine_path)

        if spine_path and (not found or found[-1] is None):
            continue  # parent dir missing — nothing to remove

        leaf_parent_hash = (
            cur_root if not spine_path else found[-1][1]
        )
        leaf_entries = dict(_read_tree_or_empty(store, leaf_parent_hash))
        if parts[-1] not in leaf_entries:
            continue  # path doesn't exist — idempotent no-op

        target_entry = leaf_entries[parts[-1]]
        affected = _collect_affected_paths(store, target_entry, raw.strip("/"))

        del leaf_entries[parts[-1]]

        if not spine_path:
            cur_root = (
                tree_mod.write_tree(store, leaf_entries)
                if leaf_entries
                else tree_mod.write_tree(store, {})
            )
        else:
            cur_root = _rebuild_spine(store, spine, spine_path, leaf_entries)

        all_changes.extend(("delete", p) for p in affected)

    if cur_root == root_hash:
        return root_hash, []
    return cur_root, all_changes


def splice_move(
    store: ObjectStore,
    root_hash: str,
    old_rel: str,
    new_rel: str,
) -> tuple[str, list[Change]]:
    """Move / rename a file or folder. Does not download blob contents.

    The source entry's ``["B"|"T", hash]`` reference is moved verbatim
    from its old location to the new one. For folders this means the
    entire subtree's tree-node + blob hashes are reused — a folder
    rename of a 1000-file subtree costs the same as a 1-file rename.

    Overwrite semantics: if ``new_rel`` already exists, it is replaced.
    The audit changes list reports the old contents as deleted and the
    new contents as added.
    """
    src_parts = _split_path(old_rel)
    dst_parts = _split_path(new_rel)
    if not src_parts or not dst_parts:
        raise ValueError("move requires non-empty source and destination")
    if src_parts == dst_parts:
        return root_hash, []
    src_clean = "/".join(src_parts)
    dst_clean = "/".join(dst_parts)
    if dst_clean.startswith(f"{src_clean}/"):
        raise ValueError(
            f"cannot move {old_rel!r} into its own subtree: {new_rel!r}",
        )

    # Stage 1: locate + remove from source.
    src_spine_path = src_parts[:-1]
    src_spine, src_found = _walk_spine(store, root_hash, src_spine_path)

    if src_spine_path and (not src_found or src_found[-1] is None):
        raise FileNotFoundError(f"source not found: {old_rel}")

    src_parent_hash = (
        root_hash if not src_spine_path else src_found[-1][1]
    )
    src_parent_entries = dict(_read_tree_or_empty(store, src_parent_hash))
    if src_parts[-1] not in src_parent_entries:
        raise FileNotFoundError(f"source not found: {old_rel}")

    moved_entry = src_parent_entries[src_parts[-1]]
    affected_old = _collect_affected_paths(store, moved_entry, old_rel.strip("/"))
    affected_new = _collect_affected_paths(store, moved_entry, new_rel.strip("/"))

    del src_parent_entries[src_parts[-1]]
    if not src_spine_path:
        intermediate = (
            tree_mod.write_tree(store, src_parent_entries)
            if src_parent_entries
            else tree_mod.write_tree(store, {})
        )
    else:
        intermediate = _rebuild_spine(
            store, src_spine, src_spine_path, src_parent_entries,
        )

    # Stage 2: insert at destination on the post-removal tree. We must
    # re-walk because src and dst may share a prefix (e.g. rename within
    # the same directory) — the spine the dst walk needs differs from
    # the src spine after the removal.
    dst_spine_path = dst_parts[:-1]
    dst_spine, dst_found = _walk_spine(store, intermediate, dst_spine_path)

    if dst_spine_path and dst_found and dst_found[-1] is not None:
        dst_parent_hash = dst_found[-1][1]
    elif not dst_spine_path:
        dst_parent_hash = intermediate
    else:
        dst_parent_hash = ""  # missing intermediate dir — will be created

    dst_parent_entries = dict(_read_tree_or_empty(store, dst_parent_hash))
    overwriting = dst_parent_entries.get(dst_parts[-1])
    overwritten_paths: list[str] = []
    if overwriting is not None:
        overwritten_paths = _collect_affected_paths(
            store, overwriting, new_rel.strip("/"),
        )
    dst_parent_entries[dst_parts[-1]] = moved_entry

    if not dst_spine_path:
        new_root = tree_mod.write_tree(store, dst_parent_entries)
    else:
        new_root = _rebuild_spine(
            store, dst_spine, dst_spine_path, dst_parent_entries,
        )

    changes: list[Change] = []
    changes.extend(("delete", p) for p in affected_old)
    changes.extend(("delete", p) for p in overwritten_paths)
    changes.extend(("add", p) for p in affected_new)
    if new_root == root_hash:
        return root_hash, []
    return new_root, changes


def splice_copy(
    store: ObjectStore,
    root_hash: str,
    old_rel: str,
    new_rel: str,
    *,
    source_root_hash: str | None = None,
) -> tuple[str, list[Change]]:
    """Copy a file or folder by reusing tree/blob hashes.

    This is the copy counterpart to :func:`splice_move`: it inserts the
    existing source entry at the destination without downloading or
    re-uploading blob bytes. If ``new_rel`` exists it is replaced and the
    overwritten paths are reported as deletes in the changes list.

    ``source_root_hash`` can bind the source to an earlier operation snapshot
    while applying the destination to the current tree (rename CAS recovery).
    Source entries are read from that snapshot, never synthesized when absent.
    """
    source_root = root_hash if source_root_hash is None else source_root_hash
    src_parts = _split_path(old_rel)
    dst_parts = _split_path(new_rel)
    if not src_parts or not dst_parts:
        raise ValueError("copy requires non-empty source and destination")
    if src_parts == dst_parts and source_root == root_hash:
        return root_hash, []

    src_spine_path = src_parts[:-1]
    _src_spine, src_found = _walk_spine(store, source_root, src_spine_path)
    if src_spine_path and (not src_found or src_found[-1] is None):
        raise FileNotFoundError(f"source not found: {old_rel}")

    src_parent_hash = source_root if not src_spine_path else src_found[-1][1]
    src_parent_entries = dict(_read_tree_or_empty(store, src_parent_hash))
    if src_parts[-1] not in src_parent_entries:
        raise FileNotFoundError(f"source not found: {old_rel}")

    copied_entry = src_parent_entries[src_parts[-1]]
    if copied_entry[0] == "T":
        src_prefix = old_rel.strip("/")
        dst_clean = new_rel.strip("/")
        if dst_clean.startswith(f"{src_prefix}/"):
            raise ValueError(
                f"cannot copy directory {old_rel!r} into itself: {new_rel!r}",
            )

    dst_spine_path = dst_parts[:-1]
    dst_spine, dst_found = _walk_spine(store, root_hash, dst_spine_path)
    if dst_spine_path and dst_found and dst_found[-1] is not None:
        dst_parent_hash = dst_found[-1][1]
    elif not dst_spine_path:
        dst_parent_hash = root_hash
    else:
        dst_parent_hash = ""

    dst_parent_entries = dict(_read_tree_or_empty(store, dst_parent_hash))
    overwriting = dst_parent_entries.get(dst_parts[-1])
    overwritten_paths: list[str] = []
    if overwriting is not None:
        overwritten_paths = _collect_affected_paths(
            store, overwriting, new_rel.strip("/"),
        )
        if overwriting == copied_entry:
            return root_hash, []

    dst_parent_entries[dst_parts[-1]] = copied_entry

    if not dst_spine_path:
        new_root = tree_mod.write_tree(store, dst_parent_entries)
    else:
        new_root = _rebuild_spine(
            store, dst_spine, dst_spine_path, dst_parent_entries,
        )

    affected_new = _collect_affected_paths(store, copied_entry, new_rel.strip("/"))
    changes: list[Change] = []
    changes.extend(("delete", p) for p in overwritten_paths)
    changes.extend(("add", p) for p in affected_new)
    if new_root == root_hash:
        return root_hash, []
    return new_root, changes


def splice_touch(
    store: ObjectStore,
    root_hash: str,
    rel_paths: Iterable[str],
) -> tuple[str, list[Change]]:
    """Record an mtime-only update for existing files.

    The tree shape and blob hashes are intentionally unchanged; callers must
    allow same-tree commits so history/audit still capture the touch event.
    """
    changes: list[Change] = []
    for raw in rel_paths:
        parts = _split_path(raw)
        if not parts:
            raise ValueError("touch requires non-empty paths")

        spine_path = parts[:-1]
        _spine, found = _walk_spine(store, root_hash, spine_path)
        if spine_path and (not found or found[-1] is None):
            raise FileNotFoundError(f"path not found: {raw}")

        parent_hash = root_hash if not spine_path else found[-1][1]
        parent_entries = _read_tree_or_empty(store, parent_hash)
        entry = parent_entries.get(parts[-1])
        if entry is None:
            raise FileNotFoundError(f"path not found: {raw}")
        if entry[0] == "T":
            raise ValueError(f"is a directory: {raw}")
        changes.append(("update", raw.strip("/")))

    return root_hash, changes


def splice_mkdir(
    store: ObjectStore,
    root_hash: str,
    rel_path: str,
) -> tuple[str, list[Change]]:
    """Create a directory at ``rel_path`` (idempotent).

    Implemented as ``splice_put_blob`` of an empty ``.keep`` placeholder
    when the directory doesn't already exist. Raises if a file currently
    occupies the path.
    """
    parts = _split_path(rel_path)
    if not parts:
        raise ValueError("cannot mkdir at empty path")

    spine_path = parts[:-1]
    _spine, found = _walk_spine(store, root_hash, spine_path)

    if spine_path and (not found or found[-1] is None):
        leaf_parent_hash = ""
    elif not spine_path:
        leaf_parent_hash = root_hash
    else:
        leaf_parent_hash = found[-1][1]

    leaf_entries = _read_tree_or_empty(store, leaf_parent_hash)
    leaf_name = parts[-1]
    existing = leaf_entries.get(leaf_name)
    if existing is not None:
        if existing[0] == "T":
            return root_hash, []  # already a directory — no-op
        raise ValueError(
            f"cannot mkdir {rel_path!r}: path currently holds a file",
        )

    return splice_put_blob(
        store, root_hash, f"{rel_path.strip('/')}/.keep", b"",
    )


def splice_multi_put_refs(
    store: ObjectStore,
    root_hash: str,
    items: list[tuple[str, str]],
) -> tuple[str, list[Change]]:
    """Apply many blob-ref puts in a SINGLE tree transformation.

    For N puts spanning M unique parent directories (M ≤ N), this
    writes:
        - exactly 1 tree node per directory along the union spine
          of all affected paths,
        - regardless of N.

    Compare with calling :func:`splice_put_blob_ref` N times in
    sequence: each per-op call rewrites every directory along ITS
    own spine, so the same parent (e.g. ``docdocs/``) ends up
    being written N times, once per leaf added — generating
    N − 1 wasted intermediate tree nodes that nobody will ever
    read. For N=3 files in one folder that's 4 wasted PUTs to
    Supabase Storage at ~1s each. This batched version eliminates
    those entirely.

    Best case (every file in the same parent dir — typical folder
    upload): 1 parent tree node + 1 root tree node, regardless of N.
    Worst case (every file in a different sibling directory at
    different depths): no worse than N × splice_put_blob_ref calls
    in terms of S3 puts; substantially fewer reads because each
    intermediate directory is read at most once.

    Items semantics:
        ``items`` is a list of ``(rel_path, blob_hash)`` tuples.
        Same-path duplicates are allowed and resolved as
        last-write-wins (matches the per-op loop semantics).
        Identical content at an existing path is a no-op (no
        tree write, no audit entry).

    Errors are raised eagerly during a parsing pass before any
    writes — a malformed input (empty path, empty hash) doesn't
    leave the store in a partially-mutated state.
    """
    if not items:
        return root_hash, []

    # Validate first; we don't want to write tree nodes only to
    # discover the 47th item has an empty path and rollback.
    parsed: list[tuple[list[str], str, str]] = []  # (parts, hash, full_path)
    for rel_path, blob_hash in items:
        parts = _split_path(rel_path)
        if not parts:
            raise ValueError("cannot put blob ref at empty path")
        if not blob_hash:
            raise ValueError("blob_hash is required for splice_multi_put_refs")
        parsed.append((parts, blob_hash, rel_path.strip("/")))

    new_root, changes = _apply_multi_put(store, root_hash, parsed)
    if new_root == root_hash:
        return root_hash, []
    return new_root, changes


def _apply_multi_put(
    store: ObjectStore,
    tree_hash: str,
    items: list[tuple[list[str], str, str]],
) -> tuple[str, list[Change]]:
    """Recursively compute the new tree hash for a directory-level
    batch of put-refs.

    Each call is responsible for ONE level in the tree:
        1. Read the current entries dict at this level.
        2. Apply leaves whose path bottoms-out here (``len(parts) == 1``).
        3. Group remaining items by their next path segment and
           recurse — each subdirectory's recursion produces a
           single new sub-hash, which we record as a single
           ``["T", h]`` entry update at this level.
        4. If anything actually changed, write the (single) new
           tree node and return its hash.

    Idempotency: if no entry actually changed (e.g. all items
    already match), we return the input ``tree_hash`` and an
    empty changes list — no tree write, no commit triggered
    upstream.

    Type-safety: writing a blob through an existing tree node, or
    writing a tree-update under an existing blob entry, both raise
    ``ValueError`` — same semantics as
    :func:`splice_put_blob_ref`.
    """
    entries = dict(_read_tree_or_empty(store, tree_hash))
    original_snapshot = dict(entries)
    changes: list[Change] = []

    # Phase A: items that bottom out at this level become leaf entries.
    # Phase B: items going deeper get bucketed by next segment.
    by_next_seg: dict[str, list[tuple[list[str], str, str]]] = {}
    for parts, blob_hash, full_path in items:
        if len(parts) == 1:
            leaf_name = parts[0]
            existing = entries.get(leaf_name)
            if existing is not None and existing[0] == "T":
                raise ValueError(
                    f"cannot write file at {full_path!r}: "
                    "path is currently a directory",
                )
            if (
                existing is not None
                and existing[0] == "B"
                and existing[1] == blob_hash
            ):
                # Idempotent: same hash already at this leaf.
                continue
            action = "add" if existing is None else "update"
            entries[leaf_name] = ["B", blob_hash]
            changes.append((action, full_path))
        else:
            next_seg = parts[0]
            remaining = parts[1:]
            by_next_seg.setdefault(next_seg, []).append(
                (remaining, blob_hash, full_path),
            )

    for next_seg, sub_items in by_next_seg.items():
        existing = entries.get(next_seg)
        if existing is not None and existing[0] == "B":
            # We can't descend through a file — but mention which one,
            # because debugging "could not descend" without the path
            # is painful.
            full = sub_items[0][2]
            raise ValueError(
                f"cannot write file at {full!r}: "
                f"path component {next_seg!r} is currently a file",
            )
        sub_tree_hash = existing[1] if existing is not None else ""
        new_sub_hash, sub_changes = _apply_multi_put(
            store, sub_tree_hash, sub_items,
        )
        if existing is None or existing[1] != new_sub_hash:
            entries[next_seg] = ["T", new_sub_hash]
        changes.extend(sub_changes)

    if entries == original_snapshot:
        return tree_hash, []

    new_tree_hash = tree_mod.write_tree(store, entries)
    return new_tree_hash, changes


def splice_batch(
    store: ObjectStore,
    root_hash: str,
    ops: list[BatchOp],
) -> tuple[str, list[Change]]:
    """Apply a sequence of mutations as one tree transformation.

    ``ops`` is a list of:
      - ``("put",     rel_path, content)``
      - ``("put_ref", rel_path, blob_hash)``
      - ``("rm",      rel_path)``
      - ``("mv",      old_rel, new_rel)``

    Each op is applied in order against the running root. The final
    root is returned with the union of all changes — bulk-write
    uses this so N writes + M deletes + K moves
    against a single scope produce one commit.

    Optimization: consecutive runs of ``put`` / ``put_ref`` ops are
    coalesced into a single :func:`splice_multi_put_refs` call, so
    a folder upload of N files in the same directory writes the
    parent tree node ONCE (not N times). Order is preserved:
    we only group within runs, never across an ``rm`` / ``mv``
    boundary. That's because reordering across a remove like
    ``put a/x; rm a; put a/y`` would produce a different end state
    (``put`` first would overwrite the other ``put``'s parent).

    Overlapping ops within a single run (e.g. ``put a`` then ``put a``
    with different bytes) are resolved last-write-wins, matching
    what the previous per-op loop did. Callers wanting dedupe
    should filter before calling.
    """
    cur_root = root_hash
    all_changes: list[Change] = []
    pending_puts: list[tuple[str, str]] = []  # (rel_path, blob_hash)

    def flush_puts():
        nonlocal cur_root
        if not pending_puts:
            return
        new_root, changes = splice_multi_put_refs(
            store, cur_root, pending_puts,
        )
        cur_root = new_root
        all_changes.extend(changes)
        pending_puts.clear()

    for op in ops:
        kind = op[0]
        if kind == "put":
            _, rel_path, content = op
            # Materialise the blob now — content-addressed and
            # idempotent, so this is cheap-ish even for repeats.
            # Then queue a put_ref for the batched spine write.
            blob_hash = tree_mod.write_blob(store, content)
            pending_puts.append((rel_path, blob_hash))
        elif kind == "put_ref":
            _, rel_path, blob_hash = op
            pending_puts.append((rel_path, blob_hash))
        elif kind == "rm":
            flush_puts()  # rm may delete an ancestor of pending puts
            _, rel_path = op
            cur_root, changes = splice_remove(store, cur_root, [rel_path])
            all_changes.extend(changes)
        elif kind == "mv":
            flush_puts()  # mv may relocate an ancestor of pending puts
            _, old_rel, new_rel = op
            cur_root, changes = splice_move(store, cur_root, old_rel, new_rel)
            all_changes.extend(changes)
        else:
            raise ValueError(f"unknown batch op: {kind!r}")

    flush_puts()

    if cur_root == root_hash:
        return root_hash, []
    return cur_root, all_changes
