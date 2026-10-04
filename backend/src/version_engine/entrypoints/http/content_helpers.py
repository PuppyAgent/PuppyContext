"""Shared helpers for content router sub-modules."""

from __future__ import annotations

import base64

from src.version_engine.entrypoints.http.schemas import VersionEntryResponse
from src.version_engine.read.tree_reader import VersionEntry
from src.platform.auth.models import CurrentUser
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService


def ensure_project_access(
    authorization: AuthorizationService,
    current_user: CurrentUser,
    project_id: str,
):
    """Authorize a human content read through the canonical Project PDP."""
    return authorization.authorize(
        project_id, current_user.user_id, ProjectAction.CONTENT_READ
    )


def ensure_write_access(
    authorization: AuthorizationService,
    current_user: CurrentUser,
    project_id: str,
):
    """Authorize a human content write through the canonical Project PDP."""
    return authorization.authorize(
        project_id, current_user.user_id, ProjectAction.CONTENT_WRITE
    )


def display_git_path(path: str) -> str:
    """JSON-safe display only; base64 fields carry non-UTF8 Git identities."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def git_path_b64(path: str) -> str:
    return base64.b64encode(path.encode("utf-8", "surrogateescape")).decode("ascii")


def entry_to_response(entry: VersionEntry) -> VersionEntryResponse:
    return VersionEntryResponse(
        name=display_git_path(entry.name),
        path=display_git_path(entry.path),
        name_bytes_b64=git_path_b64(entry.name) if entry.git_mode is not None else None,
        path_bytes_b64=git_path_b64(entry.path) if entry.git_mode is not None else None,
        git_mode=entry.git_mode,
        type=entry.type,
        content_hash=entry.content_hash,
        size_bytes=entry.size_bytes,
        mime_type=entry.mime_type,
        children_count=entry.children_count,
        integrity_status=entry.integrity_status,
    )
