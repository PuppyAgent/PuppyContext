"""Git-compatible commit projections for project and scope views."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from src.version_engine.write_engine.git_object_format import (
    EMPTY_TREE_SHA1,
    decode_commit,
    decode_tree,
)
from src.version_engine.write_engine.git_commit import build_git_commit, commit_tree_id
from src.version_engine.write_engine.path_utils import normalize_path
from src.version_engine.write_engine.tree import tree_path_modes, tree_to_flat
from src.version_engine.write_engine.tree_objects import (
    build_tree_from_blob_ids,
    is_path_excluded,
)

HEX_40 = re.compile(r"^[0-9a-f]{40}$")
GitViewHealth = Literal["empty", "healthy", "history_degraded", "current_corrupt"]


@dataclass(frozen=True)
class GitViewHead:
    """Resolved Git-visible ref state for one Access Point view."""

    head: str
    canonical_head: str
    health: GitViewHealth
    reason: str = ""
    history_cut: bool = False


def resolve_git_view_head(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
) -> GitViewHead:
    """Return the single Git-visible HEAD every transport path must use.

    PuppyOne's canonical scope/project head can contain legacy objects that are
    not safe for native Git. The Git transport exposes a projected view instead:
    current healthy content stays usable, while broken old ancestry is cut from
    the Git-visible parent chain.
    """

    canonical = _canonical_git_head_candidate(repo, scope_path)
    try:
        head = git_view_head_commit(repo, scope_path, scope_excludes)
    except Exception as exc:
        return GitViewHead(
            head="",
            canonical_head=canonical,
            health="current_corrupt",
            reason=f"current Git view cannot be projected: {exc}",
        )

    if not canonical and not head:
        return GitViewHead(head="", canonical_head="", health="empty")
    if not head:
        return GitViewHead(
            head="",
            canonical_head=canonical,
            health="current_corrupt",
            reason="current Git view has no valid Git-compatible HEAD",
        )

    if _is_history_degraded(repo, canonical, head, scope_excludes or []):
        return GitViewHead(
            head=head,
            canonical_head=canonical,
            health="history_degraded",
            reason="legacy history was projected to a Git-compatible boundary",
            history_cut=True,
        )
    return GitViewHead(head=head, canonical_head=canonical, health="healthy")


def git_view_head_commit(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
) -> str:
    scope_norm = normalize_path(scope_path)
    excludes = [normalize_path(item) for item in (scope_excludes or [])]
    if scope_norm:
        root_hash = ""
        try:
            root_hash = repo.get_root_hash() or ""
        except Exception:
            root_hash = ""
        scope_tree = _scope_tree_from_root(repo, scope_norm)
        if scope_tree:
            if excludes:
                # OID-level filter: rebuild from blob ids, never downloading
                # blob bytes. Faster (no O(N×S) blob fetch) and tolerant of a
                # damaged leaf blob — we only read tree entries, not content.
                oids = tree_to_flat(repo.store, scope_tree)
                modes = tree_path_modes(repo.store, scope_tree)
                filtered = {
                    rel_path: oid
                    for rel_path, oid in oids.items()
                    if not is_path_excluded(f"{scope_norm}/{rel_path}", excludes)
                }
                scope_tree = build_tree_from_blob_ids(repo.store, filtered, modes=modes)
            head = repo.get_scope_head_commit_id(scope_norm) or ""
            if head:
                try:
                    if commit_tree_id(repo, head) == scope_tree:
                        return git_compatible_head_commit(repo, head)
                except Exception:
                    pass
            return build_git_commit(
                repo,
                tree_sha=scope_tree,
                parent_sha="",
                who="puppyone-scope-view",
                message="Puppyone scope view",
                created_at_iso="1970-01-01T00:00:00+00:00",
            )

        if root_hash:
            # Root-first invariant: once a canonical project root exists,
            # a missing subtree means this scope is currently empty/deleted.
            # Do not resurrect stale scope-state cache rows as Git content.
            return ""

        head = repo.get_scope_head_commit_id(scope_norm) or ""
        if not head or not excludes:
            return git_compatible_head_commit(repo, head)
        filtered_tree = filtered_commit_tree(repo, head, scope_norm, excludes)
        if not filtered_tree:
            return git_compatible_head_commit(repo, head)
        return build_git_commit(
            repo,
            tree_sha=filtered_tree,
            parent_sha="",
            who="puppyone-scope-view",
            message="Puppyone scope view",
            created_at_iso="1970-01-01T00:00:00+00:00",
        )

    if not excludes and hasattr(repo, "get_latest_project_view_commit_id"):
        indexed = repo.get_latest_project_view_commit_id() or ""
        if indexed and repo.store.exists(indexed):
            return git_compatible_head_commit(repo, indexed)

    root_hash = ""
    try:
        root_hash = repo.get_root_hash() or ""
    except Exception:
        root_hash = ""
    if root_hash and excludes:
        oids = tree_to_flat(repo.store, root_hash)
        modes = tree_path_modes(repo.store, root_hash)
        filtered = {
            path: oid
            for path, oid in oids.items()
            if not is_path_excluded(path, excludes)
        }
        root_hash = build_tree_from_blob_ids(repo.store, filtered, modes=modes)

    root_scope_head = repo.get_scope_head_commit_id("") or ""
    project_head = repo.get_head_commit_id() if hasattr(repo, "get_head_commit_id") else ""
    candidate = root_scope_head or project_head
    if root_hash:
        if not candidate and root_hash == EMPTY_TREE_SHA1:
            return ""
        if candidate:
            try:
                if commit_tree_id(repo, candidate) == root_hash:
                    return git_compatible_head_commit(repo, candidate)
            except Exception:
                pass
        parent = ""
        if not excludes:
            parent = git_compatible_head_commit(repo, project_head or root_scope_head)
        return build_git_commit(
            repo,
            tree_sha=root_hash,
            parent_sha=parent,
            who="puppyone-project-view",
            message="Puppyone project view",
            created_at_iso="1970-01-01T00:00:00+00:00",
        )

    return git_compatible_head_commit(repo, candidate) if candidate else ""


def _scope_tree_from_root(repo, scope_path: str) -> str:
    try:
        root_hash = repo.get_root_hash() or ""
    except Exception:
        return ""
    if not root_hash:
        return ""
    if not scope_path:
        return root_hash
    from src.version_engine.write_engine import tree as tree_mod

    current = root_hash
    for part in [p for p in normalize_path(scope_path).split("/") if p]:
        try:
            entries = tree_mod.read_tree(repo.store, current)
        except Exception:
            return ""
        typ, child = entries.get(part, (None, None))
        if typ != "T" or not child:
            return ""
        current = child
    return current


def _canonical_git_head_candidate(repo, scope_path: str) -> str:
    scope_norm = normalize_path(scope_path)
    if scope_norm:
        head = repo.get_scope_head_commit_id(scope_norm) or ""
        return head if _is_git_object_id(head) else ""

    indexed_getter = getattr(repo, "get_latest_project_view_commit_id", None)
    if callable(indexed_getter):
        indexed = indexed_getter() or ""
        if _is_git_object_id(indexed):
            return indexed

    root_head = repo.get_scope_head_commit_id("") or ""
    if _is_git_object_id(root_head):
        return root_head
    project_head_getter = getattr(repo, "get_head_commit_id", None)
    if callable(project_head_getter):
        project_head = project_head_getter() or ""
        if _is_git_object_id(project_head):
            return project_head
    return ""


def _is_history_degraded(
    repo,
    canonical_head: str,
    git_head: str,
    scope_excludes: list[str],
) -> bool:
    if not canonical_head:
        return False
    compatible = git_compatible_head_commit(repo, canonical_head)
    if not compatible:
        return True
    if compatible != canonical_head:
        return True
    if scope_excludes:
        # A filtered Access Point naturally has a synthetic view commit even
        # when the underlying history is healthy. Do not call that degraded.
        return False
    return git_head != canonical_head


def git_compatible_head_commit(repo, commit_id: str) -> str:
    """Return a Git-parseable projection of a stored commit ancestry.

    Some imported Puppyone histories contain commit parent values that
    are not valid Git object ids. Native Git refuses to clone such commits even
    if the current tree is readable. For Git transports, rewrite only the
    incompatible ancestry into deterministic projection commits while preserving
    tree content and commit metadata.
    """

    return _git_compatible_commit(repo, commit_id, {})


def _git_compatible_commit(repo, commit_id: str, memo: dict[str, str]) -> str:
    if not _is_git_object_id(commit_id):
        return ""
    cached = memo.get(commit_id)
    if cached is not None:
        return cached

    try:
        obj_type, body = repo.store.get_object(commit_id)
    except Exception:
        memo[commit_id] = ""
        return ""
    if obj_type != "commit":
        memo[commit_id] = ""
        return ""

    try:
        info = decode_commit(body)
    except Exception:
        memo[commit_id] = ""
        return ""

    tree_id = info.get("tree", "")
    if not _is_valid_tree(repo, tree_id, set()):
        memo[commit_id] = ""
        return ""

    projected_parents: list[str] = []
    changed = False
    for parent in info.get("parents") or []:
        if not _is_git_object_id(parent):
            changed = True
            continue
        projected_parent = _git_compatible_commit(repo, parent, memo)
        if not projected_parent:
            changed = True
            continue
        projected_parents.append(projected_parent)
        if projected_parent != parent:
            changed = True

    if not changed:
        memo[commit_id] = commit_id
        return commit_id

    projected = repo.store.put_commit(_replace_commit_parents(body, projected_parents))
    memo[commit_id] = projected
    return projected


def _is_git_object_id(value: str) -> bool:
    return bool(HEX_40.match(value or ""))


def _is_valid_tree(repo, tree_id: str, seen: set[str]) -> bool:
    if not _is_git_object_id(tree_id):
        return False
    if tree_id in seen:
        return True
    seen.add(tree_id)
    try:
        obj_type, body = repo.store.get_object(tree_id)
    except Exception:
        return False
    if obj_type != "tree":
        return False
    try:
        entries = decode_tree(body)
    except Exception:
        return False
    blob_ids: list[str] = []
    for entry in entries:
        if not _is_git_object_id(entry.sha1_hex):
            return False
        if entry.is_dir:
            if not _is_valid_tree(repo, entry.sha1_hex, seen):
                return False
        elif not entry.is_gitlink:
            # Mode 160000 names a commit in another repository. Its absence
            # here is normal, not corruption of this view's object closure.
            blob_ids.append(entry.sha1_hex)
    if not blob_ids:
        return True
    try:
        return repo.store.exists_many(blob_ids) == set(blob_ids)
    except Exception:
        return False


def _replace_commit_parents(body: bytes, parents: list[str]) -> bytes:
    head, sep, message = body.partition(b"\n\n")
    lines = head.split(b"\n")
    rewritten: list[bytes] = []
    inserted = False
    for line in lines:
        if line.startswith(b"parent "):
            continue
        rewritten.append(line)
        if not inserted and line.startswith(b"tree "):
            rewritten.extend(f"parent {parent}".encode("ascii") for parent in parents)
            inserted = True
    if not sep:
        message = b""
    return b"\n".join(rewritten) + b"\n\n" + message


def filtered_commit_tree(
    repo,
    commit_id: str,
    scope_path: str,
    excludes: list[str],
) -> str:
    """Build a tree object that strips paths matching any ``excludes``.

    ``excludes`` are full repository-relative paths (the
    access-point config stores them with leading ``/``).
    :func:`is_path_excluded` normalises both sides before comparing.
    """

    tree_id = commit_tree_id(repo, commit_id)
    oids = tree_to_flat(repo.store, tree_id)
    modes = tree_path_modes(repo.store, tree_id)
    filtered = {
        rel_path: oid
        for rel_path, oid in oids.items()
        if not is_path_excluded(
            f"{scope_path}/{rel_path}" if scope_path else rel_path,
            excludes,
        )
    }
    return build_tree_from_blob_ids(repo.store, filtered, modes=modes)
