"""
Project API Schemas

Defines frontend API request/response models, matching the frontend ProjectInfo type.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectOut(BaseModel):
    """Project metadata response.

    Versioned files are intentionally not part of this contract. Directory
    listings and file reads belong to the Content API so project metadata can
    stay fast and available even when a historical content object is damaged.
    """

    id: str
    name: str
    description: str | None = None
    org_id: str
    visibility: str = "org"
    bound_git_branch: str = "main"
    updated_at: str | None = None
    access_point_count: int | None = 0
    effective_role: Literal["admin", "editor", "viewer"]
    grant_source: Literal["org_owner", "project_member", "org_visibility"]
    capabilities: list[str]


class ProjectAuthorizationOut(BaseModel):
    project_id: str
    org_id: str
    effective_role: Literal["admin", "editor", "viewer"]
    grant_source: Literal["org_owner", "project_member", "org_visibility"]
    capabilities: list[str]


class ProjectDeletionOut(BaseModel):
    """Public state of durable Project deletion; storage prefixes stay private."""

    project_id: str
    deletion_job_id: str
    status: Literal["pending", "running", "failed", "completed"]


class NativeRepositoryCreate(BaseModel):
    """Explicit new-repository profile; never a legacy activation request."""

    model_config = ConfigDict(extra="forbid")
    profile: Literal["native"]
    object_format: Literal["sha1", "sha256"] = "sha1"
    default_branch: str = Field(default="main", min_length=1, max_length=1000)

    @field_validator("default_branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        from src.version_engine.write_engine.ref_transaction import validate_ref_name

        if value.startswith("-") or value == "HEAD":
            raise ValueError("invalid initial branch")
        validate_ref_name(("refs/heads/" + value).encode("utf-8"))
        return value


class ProjectCreate(BaseModel):
    """Strict empty-Project creation request.

    Seeded/template workflows have their own explicit endpoints and are not
    compatibility fields on this operation.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    org_id: str = Field(min_length=1, max_length=200)
    repository: NativeRepositoryCreate | None = None

    @field_validator("org_id")
    @classmethod
    def normalize_org_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("org_id must not be blank")
        return normalized


class ProjectUpdate(BaseModel):
    """Update project request"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    visibility: Literal["org", "private"] | None = None
    bound_git_branch: str | None = None


class ProjectMemberOut(BaseModel):
    """Project member output"""

    id: str
    user_id: str
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    role: Literal["admin", "editor", "viewer"]
    created_at: str


class AddProjectMember(BaseModel):
    """Add project member"""

    user_id: str
    role: Literal["admin", "editor", "viewer"] = "editor"


class UpdateProjectMemberRole(BaseModel):
    """Update project member role"""

    role: Literal["admin", "editor", "viewer"]
