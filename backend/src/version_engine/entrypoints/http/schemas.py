"""
Version Engine — Data models

All data types used by the PuppyOne platform layer:
1. Tree API request/response schemas
2. Commit history, diff, and rollback schemas

Identity model:
    Commits retain the declared repository object format: legacy SHA-1
    (40 hex) or native SHA-1/SHA-256 (40/64 hex). Native read responses
    capture their target ref, expected OID and HEAD selector together with
    the content. Display paths are not byte identities; native clients use
    path_bytes_b64 for lossless paths. Legacy write requests continue to use
    an empty base_commit_id for no prior state. Native read revisions use
    expected_oid=null for an unborn selected branch. Explicit native write
    envelopes bind that captured revision and a durable caller request UUID;
    they never enroll a repository or replace an omitted legacy base.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# Path syntactic validation lives in the L4 adapter
# (``ProductOperationAdapter.*`` → ``validate_path``). Per the
# architecture doc, "syntactic cleanup, content serialization, default
# messages, and Git pack parsing are adapter-local implementation
# details, not a separate architecture layer," so request schemas stay
# minimal — they describe the wire shape only.


# ============================================================
# Tree API request schemas
# ============================================================

class NativeProductWrite(BaseModel):
    """Retry identity and genuine read revision; not an authorization grant."""
    model_config = {"extra": "forbid"}
    input_version: Literal[1] = 1
    request_key: str = Field(min_length=36, max_length=36)
    repository_revision: dict[str, Any]
    byte_paths: dict[str, str] = Field(default_factory=dict, description=(
        "Lossless path slots: path/old_path/new_path, paths/N, files/N/path. "
        "The corresponding text field must be empty. Values are canonical base64."
    ))


class WriteFileRequest(BaseModel):
    """Write file request.

    ``base_commit_id`` is an optional precondition: when provided, the
    backend atomically verifies it is still the current scope head before
    committing. A mismatch returns 409 so one-shot REST/CLI writes never
    silently overwrite another collaborator. Omit it only for callers that
    intentionally accept last-writer-wins semantics through the version
    Write Engine.
    """
    path: str
    content: Any
    message: str = ""
    base_commit_id: str | None = None
    node_type: str = "json"  # json | markdown | file
    native: NativeProductWrite | None = None


class MkdirRequest(BaseModel):
    """Create directory request"""
    path: str
    base_commit_id: str | None = None
    parents: bool = False
    native: NativeProductWrite | None = None


class MoveRequest(BaseModel):
    """Move/rename request"""
    old_path: str
    new_path: str
    message: str = ""
    base_commit_id: str | None = None
    no_clobber: bool = False
    target_directory: bool = False
    no_target_directory: bool = False
    native: NativeProductWrite | None = None


class CopyRequest(BaseModel):
    """Copy request"""
    old_path: str
    new_path: str
    message: str = ""
    base_commit_id: str | None = None
    recursive: bool = False
    no_clobber: bool = False
    target_directory: bool = False
    no_target_directory: bool = False


class TouchRequest(BaseModel):
    """Touch/create empty files request"""
    path: str = ""
    paths: list[str] | None = None
    base_commit_id: str | None = None


class RemoveRequest(BaseModel):
    """Delete request.

    Either ``path`` (single) or ``paths`` (multi-select) may be set.
    If both are set, ``paths`` wins. Deletes remove paths from the
    current tree; recovery is handled through version history/rollback.
    """
    path: str = ""
    paths: list[str] | None = None
    force: bool = False
    recursive: bool = False
    base_commit_id: str | None = None
    native: NativeProductWrite | None = None


class RmdirRequest(BaseModel):
    """Remove empty directories request."""
    path: str = ""
    paths: list[str] | None = None
    parents: bool = False
    base_commit_id: str | None = None


class BulkWriteItem(BaseModel):
    """A single file in a bulk write operation"""
    path: str
    content: Any
    node_type: str = "json"


class BulkWriteRequest(BaseModel):
    """Bulk write request; an explicit base guards the entire batch."""
    files: list[BulkWriteItem]
    message: str = ""
    base_commit_id: str | None = None
    native: NativeProductWrite | None = None


# ============================================================
# Tree API response schemas
# ============================================================

class VersionEntryResponse(BaseModel):
    """A single tree entry. Native byte-path fields, not display text, identify names."""
    name: str
    path: str
    type: str  # "folder" | "json" | "markdown" | "file"
    content_hash: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    children_count: int | None = None
    integrity_status: Literal["ok", "damaged", "unknown"] = "ok"
    git_mode: str | None = None
    name_bytes_b64: str | None = None
    path_bytes_b64: str | None = None


class RepositoryReadResponse(BaseModel):
    """Captured native ref/base; absent for the preserved legacy profile."""
    repository_revision: dict[str, Any] | None = None
    path_bytes_b64: str | None = None


class ListDirResponse(RepositoryReadResponse):
    """Response for listing directory contents"""
    path: str
    entries: list[VersionEntryResponse]
    head_commit_id: str = ""


class ReadFileResponse(RepositoryReadResponse):
    """Response for reading file contents"""
    path: str
    type: str
    content: Any = None
    content_text: str | None = None
    content_hash: str | None = None
    head_commit_id: str = ""


class StatResponse(RepositoryReadResponse):
    """File/directory information"""
    path: str
    type: str
    name: str
    content_hash: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    children_count: int | None = None
    integrity_status: Literal["ok", "damaged", "unknown"] = "ok"
    git_mode: str | None = None
    name_bytes_b64: str | None = None
    exists: bool = True
    head_commit_id: str = ""
    scope_head_commit_id: str = ""


class TreeResponse(RepositoryReadResponse):
    """Full directory tree response"""
    path: str
    entries: list[VersionEntryResponse]
    head_commit_id: str = ""


# ============================================================
# Commit history schemas
# ============================================================

class VersionCommitChange(BaseModel):
    """A single file change in a commit.

    ``action`` is the operation stored in history rows.
    ``op`` is the stable Git-style UI/API operation label.
    """
    path: str
    action: Literal["add", "update", "delete"] = "update"
    op: Literal["added", "modified", "deleted"] = "modified"


class FileVersionInfo(BaseModel):
    """History list item for a single commit."""
    commit_id: str
    parent_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]] = Field(
        default_factory=list,
    )
    who: str = ""
    message: str = ""
    changes: list[VersionCommitChange] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    root_hash: str = ""
    scope_hash: str = ""
    scope_path: str = ""
    created_at: datetime | None = None
    # Free-form metadata the engine stamped at commit time. Frontend
    # uses it to render "risky operation" warnings (mass delete /
    # bulk rename) and to surface operation-type hints in the
    # history timeline. Closes PUP-5 Gap G2 — the data has always
    # been stored on ``version_transactions``; this just plumbs it
    # back out through the read API.
    audit_detail: dict | None = None


class VersionHistoryRef(BaseModel):
    """Named Git ref included in the project history graph."""

    ref_name: str
    ref_type: Literal["branch", "tag"]
    commit_id: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class VersionHistoryResponse(BaseModel):
    """Commit history response (kept name for API compat)."""
    project_id: str
    path: str | None = None
    head_commit_id: str = ""
    root_hash: str = ""
    commits: list[FileVersionInfo]
    refs: list[VersionHistoryRef] = Field(default_factory=list)
    refs_included: bool = True
    snapshot_id: str = ""
    next_cursor: str | None = None
    has_more: bool = False
    graph_health: Literal["complete", "degraded"] = "complete"
    unreadable_commit_ids: list[
        Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    ] = Field(default_factory=list)
    total: int


class RollbackResponse(BaseModel):
    """Rollback creates a new forward-commit reverting content."""
    project_id: str
    new_commit_id: str = ""
    rolled_back_to: str = ""


class DiffItem(BaseModel):
    """A single change in a diff"""
    path: str
    old_value: Any | None = None
    new_value: Any | None = None
    change_type: str


class DiffResponse(BaseModel):
    """Diff result between two commits"""
    project_id: str = ""
    from_commit_id: str = ""
    to_commit_id: str = ""
    changes: list[DiffItem]


class RollbackRequest(BaseModel):
    """Rollback request — restore the scope to the state at
    target_commit_id by creating a new forward commit."""
    target_commit_id: str


class VersionCommitConflict(BaseModel):
    """Conflict record in a commit"""
    path: str
    strategy: str
    detail: str | None = None
    kept: str | None = None


class VersionCommitInfo(BaseModel):
    """Project-level commit record."""
    commit_id: str
    root_hash: str = ""
    scope_hash: str = ""
    scope_path: str = ""
    who: str
    message: str = ""
    changes: list[VersionCommitChange] = []
    conflicts: list[VersionCommitConflict] = []
    created_at: datetime | None = None


class VersionProjectHistoryResponse(BaseModel):
    """Project-level version commit history."""
    project_id: str
    head_commit_id: str = ""
    root_hash: str = ""
    commits: list[VersionCommitInfo]
    total: int
