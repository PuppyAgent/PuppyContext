"""Pydantic schemas for the GitHub Integration API.

Mirrors the ``github_integrations`` and ``github_sync_log`` tables
created in supabase migrations 20260509000100 / 20260509000200.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


SyncDirection = Literal["import", "export"]
SyncStatus = Literal["pending", "success", "failed", "conflict"]


# ── Integration (binding) ──────────────────────────────


class GithubIntegrationCreate(BaseModel):
    """POST /api/v1/projects/{project_id}/github/connect body."""
    oauth_connection_id: int = Field(
        ..., description="oauth_connections.id of the user's GitHub token",
    )
    github_repo_owner: str = Field(..., min_length=1)
    github_repo_name: str = Field(..., min_length=1)
    default_branch: str = Field("main", min_length=1)
    auto_import: bool = False
    webhook_secret: Optional[str] = Field(
        None, description="If auto_import=true this MUST be set",
    )


class GithubIntegrationUpdate(BaseModel):
    """PATCH /api/v1/projects/{project_id}/github body."""
    default_branch: Optional[str] = Field(None, min_length=1)
    auto_import: Optional[bool] = None
    webhook_secret: Optional[str] = None


class GithubIntegrationStatus(BaseModel):
    """GET /api/v1/projects/{project_id}/github/status response."""
    id: str
    project_id: str
    oauth_connection_id: Optional[int]
    github_repo_owner: str
    github_repo_name: str
    default_branch: str
    auto_import: bool
    has_webhook_secret: bool = Field(
        ..., description="True iff webhook_secret is set; the value never leaves the backend",
    )
    last_imported_sha: Optional[str]
    last_imported_at: Optional[datetime]
    last_exported_sha: Optional[str]
    last_exported_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


# ── Sync log ───────────────────────────────────────────


class GithubSyncLogEntry(BaseModel):
    id: str
    integration_id: str
    direction: SyncDirection
    git_sha: Optional[str]
    version_commit_id: Optional[str]
    status: SyncStatus
    error_message: Optional[str]
    files_changed: Optional[int]
    created_at: datetime


class GithubSyncLogList(BaseModel):
    integration_id: str
    entries: list[GithubSyncLogEntry]
    total: int


# ── Manual trigger ─────────────────────────────────────


class GithubImportRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/github/import body."""
    branch: Optional[str] = Field(
        None,
        description="Override default_branch for this import. Most callers leave empty.",
    )
    force: bool = Field(
        False,
        description=(
            "If True, overwrite local hash changes that haven't been "
            "exported yet. Default refuses with status='conflict' so the "
            "user explicitly opts in to data loss."
        ),
    )


class GithubExportRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/github/export body."""
    branch: Optional[str] = Field(None)
    message: Optional[str] = Field(
        None, description="Override commit message; defaults to 'Sync from Puppyone <commit_id>'",
    )


class GithubSyncRunResult(BaseModel):
    """Common response for /import and /export."""
    status: SyncStatus
    direction: SyncDirection
    git_sha: Optional[str]
    version_commit_id: Optional[str]
    files_changed: Optional[int]
    error_message: Optional[str] = None


# ── Repo discovery ─────────────────────────────────────


class GithubRepoSummary(BaseModel):
    """Minimal info shown in the UI's repo picker."""
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool


class GithubRepoList(BaseModel):
    repos: list[GithubRepoSummary]


# ── Branch discovery ──────────────────────────────────


class GithubBranchSummary(BaseModel):
    """Single row in the branch picker. ``protected`` is a hint surfaced
    to the UI so the user can avoid binding to a branch they can't push
    to without a PR. ``is_default`` lets the picker pre-select the
    repo's default branch without a second round-trip."""
    name: str
    sha: str
    protected: bool = False
    is_default: bool = False


class GithubBranchList(BaseModel):
    repo_owner: str
    repo_name: str
    branches: list[GithubBranchSummary]
