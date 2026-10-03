"""Root-first subtree grafting and legacy scope-state repair.

This module owns subtree grafting as an explicit application-layer primitive.
The project root is the source of truth for new writes. The scope-state rebuild
helpers remain for legacy rows and repair/migration paths where old scoped
commits landed before the root projection did.
"""

from __future__ import annotations

from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    TreeEntry,
    decode_tree,
    encode_tree,
)
from src.version_engine.write_engine.git_commit import is_git_object_id
from src.utils.logger import log_error, log_info, log_warning


_MAX_GRAFT_RETRIES = 5


def build_root_from_scope_state(
    repo,
    just_pushed_scope: str,
    just_pushed_hash: str,
) -> str:
    """Build a complete root tree from legacy DB scope state.

    This is no longer the normal publish path. New writes CAS the project root
    directly and update scope rows as derived caches. This helper is used only
    to recover/migrate projects that still have authoritative historical
    scope_state rows.
    """

    scopes = _valid_scope_tree_hashes(repo, repo.get_all_scope_hashes())
    if just_pushed_hash:
        pushed_tree = _usable_tree_hash(repo.store, just_pushed_hash, just_pushed_scope)
        if pushed_tree:
            scopes[just_pushed_scope] = pushed_tree
        else:
            log_warning(
                f"[root-projection] skipping just-pushed damaged scope "
                f"{just_pushed_scope!r}: tree={just_pushed_hash!r}",
            )

    root_scope_hash = scopes.get("", "")
    if root_scope_hash:
        current_root = root_scope_hash
    else:
        # Empty git tree: same canonical SHA-1 as ``git mktree </dev/null``.
        current_root = repo.store.put_tree(encode_tree([]))

    other_scopes = sorted(
        ((path, scope_hash) for path, scope_hash in scopes.items() if path and scope_hash),
        key=lambda item: (item[0].count("/"), item[0]),
    )

    for scope_path, scope_hash in other_scopes:
        try:
            current_root = graft_subtree(repo.store, current_root, scope_path, scope_hash)
        except Exception as exc:
            log_warning(
                f"[root-projection] skipping damaged scope {scope_path!r} "
                f"during root rebuild: {exc}",
            )
            continue

    return current_root


def _valid_scope_tree_hashes(repo, scope_hashes: dict[str, str]) -> dict[str, str]:
    valid: dict[str, str] = {}
    for scope_path, scope_hash in scope_hashes.items():
        tree_hash = _usable_tree_hash(repo.store, scope_hash, scope_path)
        if tree_hash:
            valid[scope_path] = tree_hash
    return valid


def _usable_tree_hash(store, tree_hash: str, scope_path: str) -> str:
    if not tree_hash:
        return ""
    if not is_git_object_id(tree_hash):
        log_warning(
            f"[root-projection] ignoring damaged scope {scope_path!r}: "
            f"tree id must be 40 hex characters, got {len(tree_hash)}",
        )
        return ""
    try:
        object_type, _body = store.get_object(tree_hash)
    except Exception as exc:
        log_warning(
            f"[root-projection] ignoring damaged scope {scope_path!r}: "
            f"tree object {tree_hash[:12]} is unreadable ({exc})",
        )
        return ""
    if object_type != "tree":
        log_warning(
            f"[root-projection] ignoring damaged scope {scope_path!r}: "
            f"object {tree_hash[:12]} is a {object_type}, expected tree",
        )
        return ""
    return tree_hash


def graft_subtree(store, old_root_hash: str, scope_path: str, new_subtree_hash: str) -> str:
    """Replace the subtree at ``scope_path`` under ``old_root_hash``.

    This is pure Git tree manipulation against the ObjectStore API. It is shared
    by product writes, Git pushes, and tests that assert scope history grafting
    remains intact.
    """

    if not scope_path:
        return new_subtree_hash
    parts = [part for part in scope_path.strip("/").split("/") if part]
    return _graft_recursive(store, old_root_hash, parts, new_subtree_hash)


def rebuild_project_root_after_commit(repo, push_result: dict) -> bool:
    """Legacy repair: rebuild project root from old scope-state commits.

    New root-first commits do not call this as their correctness path. They
    already publish the canonical root in the SQL transaction. The function
    stays for old scope-first outbox rows and one-shot repair flows.

    Concurrent pushes are naturally idempotent: every retry recomputes
    from the same registry shape, so CAS losers re-derive the same
    target root.

    Returns ``True`` on success, ``False`` if all CAS retries lost
    (project_root may then lag scope state until the next push or the
    outbox repair worker fires).
    """

    scope_hash = push_result.get("root", "")
    if not scope_hash:
        return False
    commit_id = push_result.get("commit_id") or ""
    if not commit_id:
        return False

    entry = repo.history.get_entry(commit_id) if hasattr(repo, "history") else None
    if not entry:
        log_error(f"[root-projection] no history entry for commit {commit_id}")
        return False

    scope_path = (entry.get("scope_path") or "").strip("/")

    for attempt in range(_MAX_GRAFT_RETRIES):
        try:
            if attempt > 0:
                history = getattr(repo, "history", None) or repo
                if hasattr(history, "_root_hash_cache"):
                    del history._root_hash_cache

            if hasattr(repo, "get_root_hash"):
                db_root = repo.get_root_hash() or ""
            elif hasattr(repo.history, "get_root_hash"):
                db_root = repo.history.get_root_hash() or ""
            else:
                db_root = ""

            new_root = build_root_from_scope_state(repo, scope_path, scope_hash)

            if not hasattr(repo, "cas_update_root_hash"):
                raise RuntimeError(
                    "root projection requires repo.cas_update_root_hash; "
                    "non-CAS root writes are not allowed"
                )
            success = repo.cas_update_root_hash(db_root, new_root)

            if success:
                try:
                    record_project_view_index_for_commit(
                        repo=repo,
                        entry=entry,
                        scope_path=scope_path,
                        scope_hash=scope_hash,
                        project_root_hash=new_root,
                        source_commit_id=commit_id,
                    )
                except Exception as exc:
                    log_warning(
                        f"[root-projection] project-view index update failed "
                        f"for commit {commit_id[:12]}: {exc}",
                    )
                log_info(
                    f"[root-projection] rebuilt root: scope='{scope_path}' "
                    f"root={new_root[:16]} attempt={attempt + 1}",
                )
                return True

            log_info(
                f"[root-projection] root CAS lost — retrying "
                f"(attempt {attempt + 1}, scope='{scope_path}')",
            )

        except Exception as exc:
            log_warning(
                f"[root-projection] graft attempt {attempt + 1} failed "
                f"(will retry): {exc}",
            )
            continue

    log_error(
        f"[root-projection] graft failed after {_MAX_GRAFT_RETRIES} retries "
        f"for scope='{scope_path}'; root_hash may lag scope state until the "
        f"next push or outbox repair worker fires.",
    )
    return False


def record_project_view_index_for_commit(
    *,
    repo,
    entry: dict,
    scope_path: str,
    scope_hash: str,
    project_root_hash: str,
    source_commit_id: str,
) -> None:
    """Persist the Git-visible project history graft for one scope commit.

    Writes a row to the version-index table mapping the canonical scope
    commit to its project-view counterpart (a real Git commit object
    whose tree is the freshly-grafted project root). When the
    grafted root equals the scope commit's tree (i.e. the scope IS the
    whole project), we reuse the scope commit id so ``git log`` on the
    project view shows the original commit unchanged.
    """

    if not hasattr(repo, "record_version_index"):
        return

    # Import here to avoid a circular dependency between root_projection
    # and the adapter-side commit helpers.
    from src.version_engine.write_engine.git_commit import (
        build_git_commit,
        commit_tree_id,
        shallow_git_parent_or_empty,
    )

    try:
        source_tree = commit_tree_id(repo, source_commit_id)
    except Exception:
        source_tree = ""

    if source_tree == project_root_hash:
        project_view_commit_id = source_commit_id
    else:
        parent = ""
        if hasattr(repo, "get_latest_project_view_commit_id"):
            parent = repo.get_latest_project_view_commit_id() or ""
        parent = shallow_git_parent_or_empty(repo, parent) if parent else ""
        created_at = entry.get("created_at") or entry.get("time") or ""
        project_view_commit_id = build_git_commit(
            repo,
            tree_sha=project_root_hash,
            parent_sha=parent,
            who="puppyone-project-view",
            message=f"Puppyone project view for {source_commit_id}",
            created_at_iso=created_at,
            validate_parent_graph=False,
        )

    repo.record_version_index(
        scope_path=scope_path,
        source_commit_id=source_commit_id,
        source_scope_hash=scope_hash,
        project_root_hash=project_root_hash,
        project_view_commit_id=project_view_commit_id,
    )


def _graft_recursive(store, tree_hash: str, path_parts: list[str], new_hash: str) -> str:
    obj_type, content = store.get_object(tree_hash)
    if obj_type != "tree":
        raise ValueError(
            f"_graft_recursive: object {tree_hash} is a {obj_type}, expected tree"
        )

    entries = list(decode_tree(content, object_format=store.object_format))
    target = path_parts[0]
    remaining = path_parts[1:]

    existing = next((entry for entry in entries if entry.name == target), None)
    if existing is None:
        if remaining:
            empty_tree_hash = store.put_tree(encode_tree([]))
            child_hash = _graft_recursive(store, empty_tree_hash, remaining, new_hash)
        else:
            child_hash = new_hash
    elif remaining:
        if not existing.is_dir:
            empty_tree_hash = store.put_tree(encode_tree([]))
            child_hash = _graft_recursive(store, empty_tree_hash, remaining, new_hash)
        else:
            child_hash = _graft_recursive(store, existing.sha1_hex, remaining, new_hash)
    else:
        child_hash = new_hash

    new_entries = [entry for entry in entries if entry.name != target]
    # Scope grafts always produce directory trees — the child scope's tree
    # IS a directory regardless of whether it replaces an existing leaf or
    # creates a new subtree. The previous logic guarded for "replacing a
    # leaf with another leaf hash" but that case can't arise here.
    new_entries.append(TreeEntry(
        name=target,
        mode=MODE_DIR,
        sha1_hex=child_hash,
    ))
    return store.put_tree(encode_tree(new_entries, object_format=store.object_format))
