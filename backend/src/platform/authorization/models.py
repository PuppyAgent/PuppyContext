"""Immutable types for human Project authorization.

Roles are storage facts. Capabilities are the stable product contract. Actions
are the only vocabulary routers and services may authorize.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.platform.repository_target.models import (
    RepositoryTarget,
    ResolvedRepositoryView,
)


class ProjectRole(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class GrantSource(StrEnum):
    ORG_OWNER = "org_owner"
    PROJECT_MEMBER = "project_member"
    ORG_VISIBILITY = "org_visibility"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    MACHINE = "machine"


class RuntimeMode(StrEnum):
    READ = "r"
    READ_WRITE = "rw"


class ProjectCapability(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_SETTINGS_MANAGE = "project.settings.manage"
    PROJECT_DELETE = "project.delete"
    PROJECT_MEMBER_MANAGE = "project.member.manage"
    CONTENT_READ = "content.read"
    CONTENT_WRITE = "content.write"
    HISTORY_READ = "history.read"
    HISTORY_RESTORE = "history.restore"
    AGENT_READ = "agent.read"
    AGENT_RUN = "agent.run"
    AGENT_MANAGE = "agent.manage"
    AUTOMATION_RUN = "automation.run"
    AUTOMATION_MANAGE = "automation.manage"
    SCOPE_MANAGE = "scope.manage"
    ACCESS_SURFACE_MANAGE = "access_surface.manage"
    ACCESS_SURFACE_ROTATE_SECRET = "access_surface.rotate_secret"
    SYNCHRONIZE_MANAGE = "synchronize.manage"


class ProjectAction(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_WRITE = "project.write"
    PROJECT_MANAGE = "project.settings.manage"
    PROJECT_DELETE = "project.delete"
    MEMBERS_READ = "project.member.read"
    MEMBERS_MANAGE = "project.member.manage"
    SHARE_MANAGE = "project.share.manage"
    CONTENT_READ = "content.read"
    CONTENT_WRITE = "content.write"
    HISTORY_READ = "history.read"
    HISTORY_RESTORE = "history.restore"
    AGENT_READ = "agent.read"
    AGENT_RUN = "agent.run"
    AGENT_MANAGE = "agent.manage"
    AUTOMATION_RUN = "automation.run"
    AUTOMATION_MANAGE = "automation.manage"
    ACCESS_READ = "access_surface.read"
    ACCESS_MANAGE = "access_surface.manage"
    SCOPE_MANAGE = "scope.manage"
    CREDENTIAL_MANAGE = "access_surface.rotate_secret"
    MCP_MANAGE = "mcp.manage"
    SANDBOX_MANAGE = "sandbox.manage"
    SYNCHRONIZE_MANAGE = "synchronize.manage"
    IMPORT_SOURCE_MANAGE = "import.source.manage"
    INGEST_WRITE = "ingest.write"
    TOOL_USE = "tool.use"


_VIEWER_CAPABILITIES = frozenset(
    {
        ProjectCapability.PROJECT_READ,
        ProjectCapability.CONTENT_READ,
        ProjectCapability.HISTORY_READ,
        ProjectCapability.AGENT_READ,
    }
)

_EDITOR_CAPABILITIES = _VIEWER_CAPABILITIES | frozenset(
    {
        ProjectCapability.CONTENT_WRITE,
        ProjectCapability.HISTORY_RESTORE,
        ProjectCapability.AGENT_RUN,
        ProjectCapability.AUTOMATION_RUN,
    }
)

_ADMIN_CAPABILITIES = _EDITOR_CAPABILITIES | frozenset(
    {
        ProjectCapability.PROJECT_SETTINGS_MANAGE,
        ProjectCapability.PROJECT_DELETE,
        ProjectCapability.PROJECT_MEMBER_MANAGE,
        ProjectCapability.AGENT_MANAGE,
        ProjectCapability.AUTOMATION_MANAGE,
        ProjectCapability.SCOPE_MANAGE,
        ProjectCapability.ACCESS_SURFACE_MANAGE,
        ProjectCapability.ACCESS_SURFACE_ROTATE_SECRET,
        ProjectCapability.SYNCHRONIZE_MANAGE,
    }
)

ROLE_CAPABILITIES: dict[ProjectRole, frozenset[ProjectCapability]] = {
    ProjectRole.VIEWER: _VIEWER_CAPABILITIES,
    ProjectRole.EDITOR: _EDITOR_CAPABILITIES,
    ProjectRole.ADMIN: _ADMIN_CAPABILITIES,
}

ACTION_CAPABILITY: dict[ProjectAction, ProjectCapability] = {
    ProjectAction.PROJECT_READ: ProjectCapability.PROJECT_READ,
    ProjectAction.PROJECT_WRITE: ProjectCapability.PROJECT_SETTINGS_MANAGE,
    ProjectAction.PROJECT_MANAGE: ProjectCapability.PROJECT_SETTINGS_MANAGE,
    ProjectAction.PROJECT_DELETE: ProjectCapability.PROJECT_DELETE,
    ProjectAction.MEMBERS_READ: ProjectCapability.PROJECT_MEMBER_MANAGE,
    ProjectAction.MEMBERS_MANAGE: ProjectCapability.PROJECT_MEMBER_MANAGE,
    ProjectAction.SHARE_MANAGE: ProjectCapability.PROJECT_MEMBER_MANAGE,
    ProjectAction.CONTENT_READ: ProjectCapability.CONTENT_READ,
    ProjectAction.CONTENT_WRITE: ProjectCapability.CONTENT_WRITE,
    ProjectAction.HISTORY_READ: ProjectCapability.HISTORY_READ,
    ProjectAction.HISTORY_RESTORE: ProjectCapability.HISTORY_RESTORE,
    ProjectAction.AGENT_READ: ProjectCapability.AGENT_READ,
    ProjectAction.AGENT_RUN: ProjectCapability.AGENT_RUN,
    ProjectAction.AGENT_MANAGE: ProjectCapability.AGENT_MANAGE,
    ProjectAction.AUTOMATION_RUN: ProjectCapability.AUTOMATION_RUN,
    ProjectAction.AUTOMATION_MANAGE: ProjectCapability.AUTOMATION_MANAGE,
    ProjectAction.ACCESS_READ: ProjectCapability.PROJECT_READ,
    ProjectAction.ACCESS_MANAGE: ProjectCapability.ACCESS_SURFACE_MANAGE,
    ProjectAction.SCOPE_MANAGE: ProjectCapability.SCOPE_MANAGE,
    ProjectAction.CREDENTIAL_MANAGE: ProjectCapability.ACCESS_SURFACE_ROTATE_SECRET,
    ProjectAction.MCP_MANAGE: ProjectCapability.ACCESS_SURFACE_MANAGE,
    ProjectAction.SANDBOX_MANAGE: ProjectCapability.ACCESS_SURFACE_MANAGE,
    ProjectAction.SYNCHRONIZE_MANAGE: ProjectCapability.SYNCHRONIZE_MANAGE,
    ProjectAction.IMPORT_SOURCE_MANAGE: ProjectCapability.SYNCHRONIZE_MANAGE,
    ProjectAction.INGEST_WRITE: ProjectCapability.CONTENT_WRITE,
    ProjectAction.TOOL_USE: ProjectCapability.AGENT_RUN,
}


@dataclass(frozen=True, slots=True)
class ProjectGrant:
    project_id: str
    org_id: str
    user_id: str
    role: ProjectRole
    source: GrantSource
    capabilities: frozenset[ProjectCapability]

    def allows(self, action: ProjectAction) -> bool:
        return ACTION_CAPABILITY[action] in self.capabilities

    def as_api_fields(self) -> dict[str, object]:
        return {
            "effective_role": self.role.value,
            "grant_source": self.source.value,
            "capabilities": sorted(capability.value for capability in self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class HumanPrincipal:
    user_id: str
    kind: PrincipalKind = PrincipalKind.HUMAN


@dataclass(frozen=True, slots=True)
class RuntimePrincipal:
    principal_id: str
    credential_kind: str
    kind: PrincipalKind = PrincipalKind.MACHINE


@dataclass(frozen=True, slots=True)
class RuntimeGrant:
    """Repository-view-bounded Machine data-plane grant.

    This type deliberately has no Project role or control-plane capabilities.
    """

    principal: RuntimePrincipal
    target: RepositoryTarget
    repository_view: ResolvedRepositoryView
    mode: RuntimeMode
    tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.target != self.repository_view.target:
            raise ValueError("RuntimeGrant target/view mismatch")

    @property
    def project_id(self) -> str:
        return self.target.project_id

    @property
    def path(self) -> str:
        return self.repository_view.path_prefix

    @property
    def excludes(self) -> tuple[str, ...]:
        return self.repository_view.excludes

    @property
    def can_write(self) -> bool:
        return self.mode is RuntimeMode.READ_WRITE
