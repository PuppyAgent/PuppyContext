"""Pydantic request/response DTOs for the repo redesign module.

Naming convention:
    *In   — incoming (request body)
    *Out  — outgoing (response body)
    *Patch — partial update body (all fields optional)

Routers translate Domain models (models.py) ↔ these DTOs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field



# ──────────────────────────────────────────────────────────────────────────
# Scopes
# ──────────────────────────────────────────────────────────────────────────

ModeLiteral = Literal["r", "rw"]
RoleLiteral = Literal["admin", "editor", "reader", "denied"]


class ScopeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    path: str = Field(..., min_length=1, max_length=512)
    exclude: list[str] = Field(default_factory=list)
    max_mode: ModeLiteral = "rw"


class ScopePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    exclude: Optional[list[str]] = None
    max_mode: Optional[ModeLiteral] = None


class ScopeOut(BaseModel):
    id: str
    project_id: str
    name: str
    path: str
    exclude: list[str]
    max_mode: ModeLiteral
    created_at: datetime
    updated_at: datetime


class ScopeAutoSuggestOut(BaseModel):
    """Returned by POST /scopes/auto-suggest — proposed scopes the user can
    accept individually."""

    suggestions: list[ScopeIn]


# ──────────────────────────────────────────────────────────────────────────
# Repo identity (the project's URL + prompt template)
# ──────────────────────────────────────────────────────────────────────────


class RepoIdentityScopeOut(BaseModel):
    """A scope summary embedded in the identity payload — just enough to
    render the per-scope connect URL/key block on /access."""

    id: str
    name: str
    path: str
    git_url: str


class RepoIdentityOut(BaseModel):
    project_id: str
    url: str                              # canonical credential-free root Git URL
    prompt_template: str
    scopes: list[RepoIdentityScopeOut]
    content_initialized: bool = False
    head_commit_id: Optional[str] = None


class RepoIdentityPatch(BaseModel):
    prompt_template: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────
# Permissions (team plans)
# ──────────────────────────────────────────────────────────────────────────


class PermissionIn(BaseModel):
    user_id: str
    role: RoleLiteral
    allowed_scope_ids: Optional[list[str]] = None     # None means "all scopes"


class PermissionPatch(BaseModel):
    role: Optional[RoleLiteral] = None
    allowed_scope_ids: Optional[list[str]] = None


class PermissionOut(BaseModel):
    project_id: str
    user_id: str
    role: RoleLiteral
    source: Literal["explicit", "inherited_org", "no_org_member"]
    allowed_scope_ids: Optional[list[str]]
    granted_by: Optional[str]
    granted_at: Optional[datetime]


class PermissionCheckIn(BaseModel):
    user_id: str
    action: Literal["read", "write", "admin"]
    scope_id: Optional[str] = None


class PermissionCheckOut(BaseModel):
    allowed: bool
    reason: str
