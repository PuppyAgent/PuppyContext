"""Git tree helpers for scoped transaction decisions."""

from __future__ import annotations

from src.version_engine.write_engine import tree as tree_mod
from src.version_engine.write_engine.git_object_format import (
    EMPTY_TREE_SHA1,
    MODE_DIR,
    MODE_FILE,
    TreeEntry,
    decode_tree,
    encode_tree,
)
from src.version_engine.write_engine.path_utils import normalize_path
from src.version_engine.write_engine.tree_delta import changes_from_file_maps


def flatten_tree_to_bytes(store, tree_hash: str) -> dict[str, bytes]:
    """Return ``{path: blob_bytes}`` for every file in a Git tree."""

    if not tree_hash:
        return {}
    flat_hashes = tree_mod.tree_to_flat(store, tree_hash)
    return {path: store.get(blob_hash) for path, blob_hash in flat_hashes.items()}


def build_tree_from_files(
    store, files: dict[str, bytes], *, modes: dict[str, bytes] | None = None,
) -> str:
    """Build a Git tree object from a flat ``{path: bytes}`` mapping.

    ``modes`` (``{path: mode_bytes}``, e.g. from ``tree.tree_path_modes``)
    preserves the original Git blob mode per path; absent paths default to
    a regular file. Without it, every blob is written as ``100644`` —
    silently dropping executable bits and turning symlinks into regular
    files when a tree is rebuilt from a flatten.
    """

    modes = modes or {}
    nested: dict = {}
    for path, content in files.items():
        clean = normalize_path(path)
        if not clean:
            continue
        parts = [part for part in clean.split("/") if part]
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = ("B", store.put_blob(content), modes.get(path, MODE_FILE))
    return _write_nested_tree(store, nested)


def build_tree_from_blob_ids(
    store, files: dict[str, str], *, modes: dict[str, bytes] | None = None,
) -> str:
    """Build a Git tree object from a flat ``{path: blob_object_id}`` mapping.

    Unlike :func:`build_tree_from_files`, the blobs already live in the
    content-addressed store, so we reference their object ids directly
    instead of re-uploading bytes. This lets callers rebuild a tree from
    an OID-level file map (e.g. one derived from :func:`tree_mod.tree_to_flat`)
    without ever downloading or re-putting blob contents.

    ``modes`` (``{path: mode_bytes}``) preserves the original blob mode;
    absent paths default to a regular file.
    """

    modes = modes or {}
    nested: dict = {}
    for path, blob_id in files.items():
        clean = normalize_path(path)
        if not clean or not blob_id:
            continue
        parts = [part for part in clean.split("/") if part]
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = ("B", blob_id, modes.get(path, MODE_FILE))
    return _write_nested_tree(store, nested)


def find_missing_tree_objects(store, root_hash: str) -> list[str]:
    """Return any object ids the tree at ``root_hash`` references but the store
    cannot resolve — i.e. the dangling-tree shape behind a "Damaged folder".

    Walks the tree top-down checking *existence* of every referenced subtree
    and blob. Subtree (directory) entries are read so their children are
    verified too; blob entries are existence-checked only — blob bytes are
    never downloaded. An empty result means the whole closure is present.

    This is the integrity invariant every committed root MUST satisfy: a tree
    object may only reference children that are durably present. Use it to
    assert that tree builders / grafts persist a complete closure (so a write
    can never publish a dangling tree), and as a diagnostic when auditing an
    existing root.
    """
    if not root_hash or root_hash == EMPTY_TREE_SHA1:
        return []
    missing: list[str] = []
    seen: set[str] = set()
    stack = [root_hash]
    while stack:
        tree_oid = stack.pop()
        if tree_oid in seen:
            continue
        seen.add(tree_oid)
        if not store.exists(tree_oid):
            missing.append(tree_oid)
            continue
        obj_type, body = store.get_object(tree_oid)
        if obj_type == "tree":
            stack.extend(_verify_tree_children(store, body, seen, missing))
    return missing


def _verify_tree_children(store, tree_body: bytes, seen: set, missing: list) -> list[str]:
    """Existence-check a tree's blob children and return its subtree ids.

    Subtree (directory) ids are returned for the caller to walk; blob children
    are existence-checked here (no bytes downloaded) and appended to ``missing``
    when absent.
    """
    subtrees: list[str] = []
    for entry in decode_tree(tree_body):
        if entry.is_gitlink:
            continue  # The referenced commit belongs to the submodule repository.
        child = entry.sha1_hex
        if not child or child in seen:
            continue
        if entry.is_dir:
            subtrees.append(child)  # existence verified when popped
        elif not store.exists(child):
            missing.append(child)
    return subtrees


def compute_changeset(
    scope_path: str,
    old_files: dict[str, bytes],
    new_files: dict[str, bytes],
) -> list[dict]:
    """Compute full project-root history changes for a scoped file map."""

    return changes_from_file_maps(scope_path, old_files, new_files)


def build_full_changes(
    scope_path: str,
    changes: list[tuple[str, str]],
) -> list[dict]:
    """Convert splice ``(action, rel_path)`` tuples to history rows."""

    scope_norm = normalize_path(scope_path)
    out: list[dict] = []
    for action, rel in changes:
        out.append({"path": join_scope_path(scope_norm, rel), "action": action})
    return out


def join_scope_path(scope_path: str, rel_path: str) -> str:
    scope = normalize_path(scope_path)
    rel = normalize_path(rel_path)
    if not scope:
        return rel
    if not rel:
        return scope
    return f"{scope}/{rel}"


def scope_owner_for_path(scope_paths: list[str], full_path: str) -> str:
    """Return the deepest scope path that owns ``full_path``."""

    clean = normalize_path(full_path)
    owner = ""
    for scope_path in scope_paths:
        scope = normalize_path(scope_path)
        if not scope:
            continue
        if clean == scope or clean.startswith(scope + "/"):
            if len(scope) > len(owner):
                owner = scope
    return owner


def known_scope_paths(repo) -> list[str]:
    """Best-effort list of scope paths known by definitions or state."""

    paths = {""}
    try:
        paths.update((p or "").strip("/") for p in repo.get_all_scope_hashes().keys())
    except Exception:
        pass
    try:
        for scope in repo.scopes.list_all():
            paths.add(normalize_path(scope.get("path", "")))
    except Exception:
        pass
    return sorted(paths)


def validate_scope_bound_files(
    repo,
    scope_path: str,
    rel_paths: list[str],
    scope_excludes: list[str] | None = None,
) -> list[str]:
    """Return full paths that are outside scope ownership or excluded."""

    scope_norm = normalize_path(scope_path)
    scopes = known_scope_paths(repo)
    excludes = [normalize_path(path) for path in (scope_excludes or [])]
    rejected: list[str] = []
    for rel_path in rel_paths:
        full_path = join_scope_path(scope_norm, rel_path)
        owner = scope_owner_for_path(scopes, full_path)
        if owner != scope_norm or is_path_excluded(full_path, excludes):
            rejected.append(full_path)
    return rejected


def is_path_excluded(full_path: str, excludes: list[str]) -> bool:
    """Check whether ``full_path`` lies under any ``excludes`` pattern.

    ``excludes`` are full repository-relative paths (the access-point
    config stores them with leading ``/``, e.g. ``"/docs/secret/"``;
    :func:`normalize_path` strips the slashes for comparison). Patterns
    are NOT scope-relative — to exclude ``docs/secret``, the access
    point stores ``"/docs/secret/"``, never just ``"secret"``.
    """

    clean = normalize_path(full_path)
    for excluded in excludes:
        exc = normalize_path(excluded)
        if not exc:
            continue
        if clean == exc or clean.startswith(exc + "/"):
            return True
    return False


def _write_nested_tree(store, node: dict) -> str:
    entries: list[TreeEntry] = []
    for name, val in sorted(node.items()):
        if isinstance(val, tuple):
            # Leaf: ("B", sha[, mode]). The optional 3rd element carries the
            # original blob mode (executable/symlink/gitlink); 2-tuples from
            # older call sites default to a regular file.
            kind, sub_hash = val[0], val[1]
            mode = val[2] if len(val) > 2 else MODE_FILE
            entries.append(TreeEntry(
                name=name,
                mode=mode if kind == "B" else MODE_DIR,
                sha1_hex=sub_hash,
            ))
        else:
            sub_hash = _write_nested_tree(store, val)
            entries.append(TreeEntry(name=name, mode=MODE_DIR, sha1_hex=sub_hash))
    return store.put_tree(encode_tree(entries))
