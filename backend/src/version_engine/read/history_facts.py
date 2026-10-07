"""Narrow readers for canonical project-head and immutable Git commit facts."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from src.utils.logger import log_error
from src.version_engine.read.history_models import GraphCommit
from src.version_engine.write_engine.git_object_format import (
    decode_commit,
    is_git_object_id,
    split_author_line,
)


def read_commit_parent_ids(repo, commit_ids: list[str]) -> dict[str, list[str]]:
    parents_by_commit: dict[str, list[str]] = {}
    objects: dict[str, tuple[str, bytes]] = {}
    batch_reader = getattr(repo.store, "get_objects_many", None)
    valid_ids = [oid for oid in dict.fromkeys(commit_ids) if is_git_object_id(oid)]
    if callable(batch_reader):
        # Bound each batch, and preserve partial/degraded semantics if an
        # object is missing or corrupt. No second cache or unverified decoder.
        for offset in range(0, len(valid_ids), 100):
            with contextlib.suppress(Exception):
                objects.update(batch_reader(valid_ids[offset : offset + 100]))
    for commit_id in dict.fromkeys(commit_ids):
        node = read_graph_commit(repo, commit_id, object_data=objects.get(commit_id))
        parents_by_commit[commit_id] = list(node.parent_ids) if node else []
    return parents_by_commit


def read_graph_commit(
    repo, commit_id: str, *, object_data: tuple[str, bytes] | None = None
) -> GraphCommit | None:
    """Decode one immutable commit object without consulting history metadata."""

    if not is_git_object_id(commit_id):
        return None
    try:
        obj_type, content = (
            object_data if object_data is not None else repo.store.get_object(commit_id)
        )
        if obj_type != "commit":
            raise ValueError(f"expected commit object, got {obj_type}")
        info = decode_commit(content)
        tree_id = str(info.get("tree") or "")
        if not is_git_object_id(tree_id):
            raise ValueError("commit has an invalid tree id")
        raw_parent_ids = tuple(info.get("parents") or ())
        if not all(is_git_object_id(parent_id) for parent_id in raw_parent_ids):
            raise ValueError("commit has an invalid parent id")
    except Exception as exc:
        log_error(f"[history-facts] cannot read commit {commit_id}: {exc}")
        return None

    parent_ids = tuple(dict.fromkeys(raw_parent_ids))
    timestamp, created_at = _git_identity_time(info.get("committer") or info.get("author") or "")
    author_identity, _author_time = split_author_line(info.get("author") or "")
    author = author_identity.rsplit("<", 1)[0].strip() or author_identity.strip() or "Git"
    message_lines = (info.get("message") or "").splitlines()
    message = (message_lines[0].strip() if message_lines else "") or "Update workspace"
    return GraphCommit(
        commit_id=commit_id,
        parent_ids=parent_ids,
        tree_id=tree_id,
        author=author,
        message=message,
        created_at=created_at,
        timestamp=timestamp,
    )


def _git_identity_time(identity_line: str) -> tuple[int, str | None]:
    _identity, raw_time = split_author_line(identity_line)
    try:
        timestamp = int(raw_time.split(" ", 1)[0])
    except (TypeError, ValueError):
        return 0, None
    try:
        created_at = datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return timestamp, None
    return timestamp, created_at
