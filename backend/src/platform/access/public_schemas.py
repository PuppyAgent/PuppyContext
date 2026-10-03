"""Canonical Access resource contracts; legacy storage names stay internal."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.platform.repository_target.schemas import RepositoryTargetSchema

AccessKind = Literal[
    "git_remote", "cli", "agent", "mcp", "mcp_endpoint", "sandbox", "sandbox_endpoint"
]
AccessDirection = Literal["bidirectional", "inbound", "outbound"]


class AccessSurface(BaseModel):
    id: str
    project_id: str
    target: RepositoryTargetSchema
    kind: AccessKind
    name: str | None = None
    status: str
    direction: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    oauth_connection_id: int | None = None
    trigger: dict[str, Any] | None = None
    last_activity_at: datetime | str | None = None
    error_message: str | None = None
    created_by: str | None = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    # Enriched inventory facts are optional, never guessed by other views.
    path: str | None = None
    node_name: str | None = None
    has_key: bool | None = None
    key_last4: str | None = None


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessTrigger(StrictRequest):
    type: Literal["manual", "scheduled", "on_change"] = "manual"
    config: dict[str, Any] | None = None


class AccessTargetRequest(StrictRequest):
    target: RepositoryTargetSchema

    @field_validator("target", mode="before")
    @classmethod
    def unambiguous_target(cls, value):
        if isinstance(value, dict):
            allowed = {"kind", "project_id"}
            if value.get("kind") == "scope":
                allowed.add("scope_id")
            if set(value) - allowed or any(
                not isinstance(value.get(key), str) or not value[key].strip() for key in allowed
            ):
                raise ValueError("Repository target must have explicit, non-empty canonical fields")
        return value


class AccessSurfaceCreate(AccessTargetRequest):
    kind: AccessKind
    direction: AccessDirection
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    oauth_connection_id: int | None = None
    trigger: AccessTrigger = Field(default_factory=AccessTrigger)


class AccessSurfaceUpdate(StrictRequest):
    name: str | None = None
    direction: AccessDirection | None = None
    config: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    oauth_connection_id: int | None = None
    trigger: AccessTrigger | None = None
    status: Literal["active", "paused"] | None = None


class AccessSurfaceMetadataUpdate(StrictRequest):
    status: Literal["active", "paused"] | None = None
    trigger: AccessTrigger | None = None
    config: dict[str, Any] | None = None


class AccessTargetEnable(AccessTargetRequest):
    pass


class AccessSurfaceRename(StrictRequest):
    name: str


class AccessSurfaceConfigure(StrictRequest):
    """Existing adapter-backed creation, not source/provider configuration."""

    project_id: str = Field(min_length=1)
    kind: Literal["agent", "mcp", "sandbox"]
    name: str | None = None
    path: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    accesses: list[dict[str, Any]] | None = None
    tools_config: list[dict[str, Any]] | None = None


class AccessSurfaceCreated(BaseModel):
    """Explicit one-time issuance; never reuse as an ordinary metadata DTO."""

    id: str
    project_id: str
    kind: AccessKind
    name: str | None = None
    status: str
    target: RepositoryTargetSchema | None = None
    mcp_api_key: str | None = None
    mcp_server_url: str | None = None


class AccessCredentialIssued(BaseModel):
    access_surface_id: str
    credential: str
    target: RepositoryTargetSchema | None = None
    credential_hint: str | None = None


class AccessSurfaceKind(BaseModel):
    kind: AccessKind
    display_name: str
    description: str
    auth: str
    creation_mode: str
    category: Literal["access"]
    icon: str
