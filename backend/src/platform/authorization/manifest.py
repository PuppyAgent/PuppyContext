"""Auditable authorization contract for every Project-scoped HTTP route.

The manifest is intentionally independent from router implementation. CI
compares it with FastAPI's route table so a newly-added Project endpoint cannot
ship without an explicit Human Project action or Machine RuntimeGrant class.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.platform.authorization.models import ProjectAction


class AuthorizationPlane(StrEnum):
    HUMAN_PROJECT = "human_project"
    HUMAN_RESOURCE_OWNER = "human_resource_owner"
    MACHINE_RUNTIME = "machine_runtime"


@dataclass(frozen=True, slots=True)
class RouteAuthorizationContract:
    plane: AuthorizationPlane
    action: ProjectAction | str


def _human(action: ProjectAction) -> RouteAuthorizationContract:
    return RouteAuthorizationContract(AuthorizationPlane.HUMAN_PROJECT, action)


def _runtime(action: str) -> RouteAuthorizationContract:
    return RouteAuthorizationContract(AuthorizationPlane.MACHINE_RUNTIME, action)


def _owner(action: str) -> RouteAuthorizationContract:
    return RouteAuthorizationContract(
        AuthorizationPlane.HUMAN_RESOURCE_OWNER, action
    )


PROJECT_ROUTE_AUTHORIZATION: dict[
    tuple[str, str], RouteAuthorizationContract
] = {
    # Project control plane.
    ("GET", "/api/v1/projects/{project_id}"): _human(ProjectAction.PROJECT_READ),
    ("GET", "/api/v1/projects/{project_id}/authorization"): _human(ProjectAction.PROJECT_READ),
    ("GET", "/api/v1/projects/{project_id}/dashboard"): _human(ProjectAction.PROJECT_READ),
    ("GET", "/api/v1/projects/{project_id}/readiness"): _human(ProjectAction.PROJECT_READ),
    ("GET", "/api/v1/projects/{project_id}/git-view/health"): _human(ProjectAction.PROJECT_READ),
    ("POST", "/api/v1/projects/{project_id}/git-view/rebuild-cache"): _human(ProjectAction.PROJECT_MANAGE),
    ("PUT", "/api/v1/projects/{project_id}"): _human(ProjectAction.PROJECT_MANAGE),
    ("DELETE", "/api/v1/projects/{project_id}"): _human(ProjectAction.PROJECT_DELETE),
    (
        "POST",
        "/api/v1/projects/{project_id}/initialization/abandon",
    ): _owner("project_initialization.operation_owner"),
    ("POST", "/api/v1/projects/{project_id}/seed"): _human(ProjectAction.CONTENT_WRITE),
    ("GET", "/api/v1/projects/{project_id}/members"): _human(ProjectAction.MEMBERS_READ),
    ("POST", "/api/v1/projects/{project_id}/members"): _human(ProjectAction.MEMBERS_MANAGE),
    ("PUT", "/api/v1/projects/{project_id}/members/{target_user_id}/role"): _human(ProjectAction.MEMBERS_MANAGE),
    ("DELETE", "/api/v1/projects/{project_id}/members/{target_user_id}"): _human(ProjectAction.MEMBERS_MANAGE),
    ("GET", "/api/v1/projects/{project_id}/share"): _human(ProjectAction.SHARE_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/share/rotate"): _human(ProjectAction.SHARE_MANAGE),

    # A human ProjectGrant may mint a user-owned credential for one exact Git
    # target. The credential is a data-plane principal, not a local checkout.
    ("POST", "/api/v1/projects/{project_id}/git-credentials"): _human(ProjectAction.CONTENT_READ),

    # Content, History and conflict surfaces.
    # Managed Office sessions are resource-owner operations, not project grants.
    # Engine callbacks/source reads use scoped capability verification.
    ("GET", "/api/v1/office/sessions/{session_id}"): _owner("office.session_owner"),
    ("DELETE", "/api/v1/office/sessions/{session_id}"): _owner("office.session_owner"),
    ("POST", "/api/v1/office/sessions/{session_id}/force-save"): _owner("office.session_owner"),
    ("GET", "/api/v1/office/sessions/{session_id}/result"): _owner("office.session_owner"),
    ("GET", "/api/v1/office/engine/sessions/{session_id}/source/{filename}"): _runtime("office.source_capability"),
    ("POST", "/api/v1/office/engine/sessions/{session_id}/callback"): _runtime("office.callback_capability"),
    **{
        ("GET", f"/api/v1/content/{{project_id}}/{suffix}"): _human(action)
        for suffix, action in {
            "cat": ProjectAction.CONTENT_READ,
            "download": ProjectAction.CONTENT_READ,
            "inline": ProjectAction.CONTENT_READ,
            "ls": ProjectAction.CONTENT_READ,
            "raw": ProjectAction.CONTENT_READ,
            "stat": ProjectAction.CONTENT_READ,
            "tree": ProjectAction.CONTENT_READ,
            "commit-content": ProjectAction.HISTORY_READ,
            "commits": ProjectAction.HISTORY_READ,
            "head": ProjectAction.HISTORY_READ,
            "conflicts/pending": ProjectAction.HISTORY_READ,
            "diff": ProjectAction.HISTORY_READ,
        }.items()
    },
    ("GET", "/api/v1/content/{project_id}/conflicts/{pending_conflict_id}"): _human(ProjectAction.HISTORY_READ),
    ("POST", "/api/v1/content/{project_id}/download/sign"): _human(ProjectAction.CONTENT_READ),
    ("POST", "/api/v1/content/{project_id}/inline/sign"): _human(ProjectAction.CONTENT_READ),
    **{
        ("POST", f"/api/v1/content/{{project_id}}/{suffix}"): _human(ProjectAction.CONTENT_WRITE)
        for suffix in ("bulk-write", "mkdir", "mv", "rm", "write")
    },
    ("POST", "/api/v1/content/{project_id}/rollback"): _human(ProjectAction.HISTORY_RESTORE),
    ("POST", "/api/v1/content/{project_id}/conflicts/{pending_conflict_id}/resolve"): _human(ProjectAction.HISTORY_RESTORE),

    # Runtime-surface administration.
    ("GET", "/api/v1/projects/{project_id}/scopes"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/projects/{project_id}/scopes"): _human(ProjectAction.SCOPE_MANAGE),
    ("PATCH", "/api/v1/projects/{project_id}/scopes/{scope_id}"): _human(ProjectAction.SCOPE_MANAGE),
    ("DELETE", "/api/v1/projects/{project_id}/scopes/{scope_id}"): _human(ProjectAction.SCOPE_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/scopes/auto-suggest"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/projects/{project_id}/access-point"): _human(ProjectAction.ACCESS_READ),
    ("PATCH", "/api/v1/projects/{project_id}/access-point"): _human(ProjectAction.PROJECT_MANAGE),
    ("GET", "/api/v1/projects/{project_id}/connectors"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/projects/{project_id}/connectors"): _human(ProjectAction.ACCESS_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/connectors/enable-target"): _human(ProjectAction.ACCESS_MANAGE),
    ("PATCH", "/api/v1/projects/{project_id}/connectors/{connector_id}"): _human(ProjectAction.ACCESS_MANAGE),
    ("DELETE", "/api/v1/projects/{project_id}/connectors/{connector_id}"): _human(ProjectAction.ACCESS_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/connectors/{connector_id}/activate-agent"): _human(ProjectAction.AGENT_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/connectors/{connector_id}/pause"): _human(ProjectAction.ACCESS_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/connectors/{connector_id}/resume"): _human(ProjectAction.ACCESS_MANAGE),
    ("POST", "/api/v1/projects/{project_id}/connectors/{connector_id}/run"): _human(ProjectAction.AUTOMATION_RUN),
    ("GET", "/api/v1/tools/by-project/{project_id}"): _human(ProjectAction.CONTENT_READ),

    # GitHub is a Project integration; reads may be viewed, mutations are Admin.
    **{
        ("GET", f"/api/v1/projects/{{project_id}}/github/{suffix}"): _human(ProjectAction.ACCESS_READ)
        for suffix in ("branches", "repos", "status", "sync-log")
    },
    ("DELETE", "/api/v1/projects/{project_id}/github"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("PATCH", "/api/v1/projects/{project_id}/github"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    **{
        ("POST", f"/api/v1/projects/{{project_id}}/github/{suffix}"): _human(ProjectAction.SYNCHRONIZE_MANAGE)
        for suffix in ("connect", "export", "import")
    },

    # Git smart HTTP is the Machine data plane. These routes must never accept
    # Project roles as a substitute for their scoped RuntimeGrant.
    ("GET", "/git/{project_id}.git/health"): _runtime("git.health"),
    ("GET", "/git/{project_id}.git/info/refs"): _runtime("git.read"),
    ("POST", "/git/{project_id}.git/git-upload-pack"): _runtime("git.read"),
    ("POST", "/git/{project_id}.git/git-receive-pack"): _runtime("git.write"),
    ("POST", "/git/{project_id}.git/rebuild-cache"): _runtime("git.admin"),
    ("GET", "/git/{project_id}/scopes/{scope_id}.git/health"): _runtime("git.health"),
    ("GET", "/git/{project_id}/scopes/{scope_id}.git/info/refs"): _runtime("git.read"),
    ("POST", "/git/{project_id}/scopes/{scope_id}.git/git-upload-pack"): _runtime("git.read"),
    ("POST", "/git/{project_id}/scopes/{scope_id}.git/git-receive-pack"): _runtime("git.write"),
    ("POST", "/git/{project_id}/scopes/{scope_id}.git/rebuild-cache"): _runtime("git.admin"),
}

# Query/body/child-resource routes are listed as deliberately as path-scoped
# routes. A Project id need not appear in the URL for a route to cross a
# Project boundary: Agent, Tool, Integration, and other resource ids derive one.
PROJECT_ROUTE_AUTHORIZATION.update({
    # Public-link administration exposes Project content outside the tenant.
    # The opaque /p/{publish_key} reader is a separate public credential plane.
    ("POST", "/api/v1/publishes/"): _human(ProjectAction.SHARE_MANAGE),
    ("GET", "/api/v1/publishes/"): _human(ProjectAction.SHARE_MANAGE),
    ("PATCH", "/api/v1/publishes/{publish_id}"): _human(ProjectAction.SHARE_MANAGE),
    ("DELETE", "/api/v1/publishes/{publish_id}"): _human(ProjectAction.SHARE_MANAGE),

    # Legacy table facade over Project content.
    ("GET", "/api/v1/tables/"): _human(ProjectAction.CONTENT_READ),
    ("POST", "/api/v1/tables/"): _human(ProjectAction.CONTENT_WRITE),
    ("GET", "/api/v1/tables/{table_id}"): _human(ProjectAction.CONTENT_READ),
    ("PUT", "/api/v1/tables/{table_id}"): _human(ProjectAction.CONTENT_WRITE),
    ("DELETE", "/api/v1/tables/{table_id}"): _human(ProjectAction.CONTENT_WRITE),
    ("GET", "/api/v1/tables/{table_id}/data"): _human(ProjectAction.CONTENT_READ),
    ("POST", "/api/v1/tables/{table_id}/data"): _human(ProjectAction.CONTENT_WRITE),
    ("PUT", "/api/v1/tables/{table_id}/data"): _human(ProjectAction.CONTENT_WRITE),
    ("DELETE", "/api/v1/tables/{table_id}/data"): _human(ProjectAction.CONTENT_WRITE),

    # Tools and search indexes derive Project from path/tool id.
    ("GET", "/api/v1/tools/"): _human(ProjectAction.AGENT_READ),
    ("POST", "/api/v1/tools/"): _human(ProjectAction.AGENT_MANAGE),
    ("GET", "/api/v1/tools/by-path/{path:path}"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/tools/{tool_id}"): _human(ProjectAction.AGENT_READ),
    ("PUT", "/api/v1/tools/{tool_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("DELETE", "/api/v1/tools/{tool_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("POST", "/api/v1/tools/search"): _human(ProjectAction.AGENT_MANAGE),
    ("GET", "/api/v1/tools/{tool_id}/search-index"): _human(ProjectAction.AGENT_READ),

    # Agent control plane and chat sessions.
    ("POST", "/api/v1/agents"): _human(ProjectAction.AGENT_RUN),
    ("GET", "/api/v1/agent-config/"): _human(ProjectAction.AGENT_READ),
    ("GET", "/api/v1/agent-config/default"): _human(ProjectAction.AGENT_READ),
    ("GET", "/api/v1/agent-config/{agent_id}"): _human(ProjectAction.AGENT_READ),
    ("POST", "/api/v1/agent-config/"): _human(ProjectAction.AGENT_MANAGE),
    ("PUT", "/api/v1/agent-config/{agent_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("DELETE", "/api/v1/agent-config/{agent_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("POST", "/api/v1/agent-config/{agent_id}/bash"): _human(ProjectAction.AGENT_MANAGE),
    ("PUT", "/api/v1/agent-config/{agent_id}/bash"): _human(ProjectAction.AGENT_MANAGE),
    ("PUT", "/api/v1/agent-config/{agent_id}/bash/{bash_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("DELETE", "/api/v1/agent-config/{agent_id}/bash/{bash_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("GET", "/api/v1/agent-config/{agent_id}/executions"): _human(ProjectAction.AGENT_READ),
    ("POST", "/api/v1/chat/sessions"): _human(ProjectAction.AGENT_RUN),
    ("GET", "/api/v1/chat/sessions"): _human(ProjectAction.AGENT_READ),
    ("GET", "/api/v1/chat/sessions/{session_id}"): _human(ProjectAction.AGENT_READ),
    ("PATCH", "/api/v1/chat/sessions/{session_id}"): _human(ProjectAction.AGENT_RUN),
    ("DELETE", "/api/v1/chat/sessions/{session_id}"): _human(ProjectAction.AGENT_RUN),
    ("GET", "/api/v1/chat/sessions/{session_id}/messages"): _human(ProjectAction.AGENT_READ),
    ("GET", "/api/v1/mcp/agents/{agent_id}/status"): _human(ProjectAction.AGENT_READ),
    ("POST", "/api/v1/mcp/agents/{agent_id}/regenerate-key"): _human(ProjectAction.CREDENTIAL_MANAGE),
    ("GET", "/api/v1/mcp/agents/{agent_id}/tools"): _human(ProjectAction.AGENT_READ),
    ("POST", "/api/v1/mcp/agents/{agent_id}/tools"): _human(ProjectAction.AGENT_MANAGE),
    ("PUT", "/api/v1/mcp/agents/{agent_id}/tools/{tool_id}"): _human(ProjectAction.AGENT_MANAGE),
    ("DELETE", "/api/v1/mcp/agents/{agent_id}/tools/{tool_id}"): _human(ProjectAction.AGENT_MANAGE),

    ("POST", "/api/v1/projects/{project_id}/repository-context"): _human(ProjectAction.PROJECT_READ),
    ("DELETE", "/api/v1/projects/{project_id}/git-credentials/{credential_id}"): _owner("git_credential.owner"),

    # Delegated internal human calls.
    ("POST", "/internal/nodes/resolve-path"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/internal/nodes/list"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/internal/nodes/read"): _human(ProjectAction.CONTENT_READ),
    ("PUT", "/internal/nodes/write"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/internal/nodes/create"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/internal/nodes/rm"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/internal/nodes/rename"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/internal/nodes/move"): _human(ProjectAction.CONTENT_WRITE),
    ("GET", "/internal/table/{table_id}"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/internal/tables/{table_id}/context-schema"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/internal/tables/{table_id}/context-data"): _human(ProjectAction.CONTENT_READ),
    ("POST", "/internal/tables/{table_id}/context-data"): _human(ProjectAction.CONTENT_WRITE),
    ("PUT", "/internal/tables/{table_id}/context-data"): _human(ProjectAction.CONTENT_WRITE),
    ("DELETE", "/internal/tables/{table_id}/context-data"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/internal/tools/{tool_id}/search"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/nodes/project-audit-logs"): _human(ProjectAction.HISTORY_READ),
    ("GET", "/api/v1/nodes/{path:path}/audit-logs"): _human(ProjectAction.HISTORY_READ),

    # Local snapshots and remote Agent workspaces.
    ("POST", "/api/v1/local-snapshots"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/local-snapshots"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/local-snapshots/{snapshot_id}"): _human(ProjectAction.CONTENT_READ),
    ("DELETE", "/api/v1/local-snapshots/{snapshot_id}"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/api/v1/local-snapshots/{snapshot_id}/blobs"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/api/v1/local-snapshots/{snapshot_id}/promote"): _human(ProjectAction.CONTENT_WRITE),
    ("POST", "/api/v1/workspace/create"): _human(ProjectAction.AGENT_RUN),
    ("POST", "/api/v1/workspace/{agent_id}/complete"): _human(ProjectAction.CONTENT_WRITE),
    ("GET", "/api/v1/workspace/{agent_id}/status"): _human(ProjectAction.AGENT_READ),
})

PROJECT_ROUTE_AUTHORIZATION.update({
    # Integration control and execution.
    ("GET", "/api/v1/integrations/status"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/integrations/connections"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("GET", "/api/v1/integrations/connections"): _human(ProjectAction.ACCESS_READ),
    ("DELETE", "/api/v1/integrations/connections/{connection_id}"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("PATCH", "/api/v1/integrations/connections/{connection_id}"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("PATCH", "/api/v1/integrations/connections/{connection_id}/trigger"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("POST", "/api/v1/integrations/connections/{connection_id}/pause"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("POST", "/api/v1/integrations/connections/{connection_id}/refresh"): _human(ProjectAction.AUTOMATION_RUN),
    ("POST", "/api/v1/integrations/connections/{connection_id}/resume"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("GET", "/api/v1/integrations/failed-runs"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/integrations/connections/{connection_id}/runs"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/integrations/runs/{run_id}"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/integrations/bootstrap"): _human(ProjectAction.SYNCHRONIZE_MANAGE),
    ("POST", "/api/v1/integrations/pull"): _human(ProjectAction.AUTOMATION_RUN),
    ("POST", "/api/v1/integrations/push/{path:path}"): _human(ProjectAction.AUTOMATION_RUN),
    ("POST", "/api/v1/integrations/github/webhook"): _runtime("integration.webhook"),

    # Scope policy, Sandbox sessions, analytics and activity.
    ("POST", "/api/v1/scope-sandboxes/connect"): _human(ProjectAction.SANDBOX_MANAGE),
    ("GET", "/api/v1/scope-sandboxes/status"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/scope-sandboxes/revoke"): _human(ProjectAction.SANDBOX_MANAGE),
    ("GET", "/api/v1/scope-sync/policy"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/scope-sync/events"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/scope-sync/activity"): _human(ProjectAction.HISTORY_READ),
    ("GET", "/api/v1/scope-sync/stats"): _human(ProjectAction.HISTORY_READ),
    ("GET", "/api/v1/scope-sync/settings"): _human(ProjectAction.CONTENT_READ),
    ("PUT", "/api/v1/scope-sync/settings"): _human(ProjectAction.SCOPE_MANAGE),
    ("GET", "/api/v1/scope-sync/ap/events"): _runtime("scope_sync.read"),
    ("GET", "/api/v1/analytics/access-timeseries"): _human(ProjectAction.HISTORY_READ),
    ("GET", "/api/v1/analytics/access-summary"): _human(ProjectAction.HISTORY_READ),
    ("GET", "/api/v1/activity"): _human(ProjectAction.HISTORY_READ),
})

PROJECT_ROUTE_AUTHORIZATION.update({
    # Upload/import tasks re-resolve ProjectGrant or user-bound upload ownership.
    ("POST", "/api/v1/ingest/submit/file"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/ingest/upload/init"): _human(ProjectAction.INGEST_WRITE),
    ("PUT", "/api/v1/ingest/upload/part"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/ingest/upload/complete"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/ingest/upload/complete-batch"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/ingest/upload/abort"): _owner("upload.owner"),
    ("POST", "/api/v1/ingest/submit/saas"): _human(ProjectAction.INGEST_WRITE),
    ("GET", "/api/v1/ingest/tasks/{task_id}"): _human(ProjectAction.CONTENT_READ),
    ("POST", "/api/v1/ingest/tasks/batch"): _human(ProjectAction.CONTENT_READ),
    ("DELETE", "/api/v1/ingest/tasks/{task_id}"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/upload/submit/file"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/upload/init"): _human(ProjectAction.INGEST_WRITE),
    ("PUT", "/api/v1/upload/part"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/upload/complete"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/upload/complete-batch"): _human(ProjectAction.INGEST_WRITE),
    ("POST", "/api/v1/upload/abort"): _owner("upload.owner"),
    ("POST", "/api/v1/imports"): _human(ProjectAction.INGEST_WRITE),
    ("GET", "/api/v1/imports"): _human(ProjectAction.CONTENT_READ),
    ("GET", "/api/v1/imports/{job_id}"): _human(ProjectAction.CONTENT_READ),
    ("DELETE", "/api/v1/imports/{job_id}"): _human(ProjectAction.INGEST_WRITE),

    # Database connectors.
    ("POST", "/api/v1/db-connector/access"): _human(ProjectAction.IMPORT_SOURCE_MANAGE),
    ("GET", "/api/v1/db-connector/access"): _human(ProjectAction.ACCESS_READ),
    ("DELETE", "/api/v1/db-connector/access/{connection_id}"): _human(ProjectAction.IMPORT_SOURCE_MANAGE),
    ("GET", "/api/v1/db-connector/access/{connection_id}/tables"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/db-connector/access/{connection_id}/tables/{table_name}/preview"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/db-connector/access/{connection_id}/save"): _human(ProjectAction.CONTENT_WRITE),
})

PROJECT_ROUTE_AUTHORIZATION.update({
    # MCP, Sandbox and unified Access administration.
    ("GET", "/api/v1/mcp-endpoints"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/mcp-endpoints/{endpoint_id}"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/mcp-endpoints/by-path/{path:path}"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/mcp-endpoints"): _human(ProjectAction.MCP_MANAGE),
    ("PUT", "/api/v1/mcp-endpoints/{endpoint_id}"): _human(ProjectAction.MCP_MANAGE),
    ("DELETE", "/api/v1/mcp-endpoints/{endpoint_id}"): _human(ProjectAction.MCP_MANAGE),
    ("POST", "/api/v1/mcp-endpoints/{endpoint_id}/regenerate-key"): _human(ProjectAction.CREDENTIAL_MANAGE),
    ("GET", "/api/v1/sandbox-endpoints"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/sandbox-endpoints/{endpoint_id}"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/sandbox-endpoints/by-path/{path:path}"): _human(ProjectAction.ACCESS_READ),
    ("POST", "/api/v1/sandbox-endpoints"): _human(ProjectAction.SANDBOX_MANAGE),
    ("PUT", "/api/v1/sandbox-endpoints/{endpoint_id}"): _human(ProjectAction.SANDBOX_MANAGE),
    ("DELETE", "/api/v1/sandbox-endpoints/{endpoint_id}"): _human(ProjectAction.SANDBOX_MANAGE),
    ("POST", "/api/v1/sandbox-endpoints/{endpoint_id}/regenerate-key"): _human(ProjectAction.CREDENTIAL_MANAGE),
    ("POST", "/api/v1/sandbox-endpoints/{endpoint_id}/exec"): _runtime("sandbox.exec"),
    ("GET", "/api/v1/access/"): _human(ProjectAction.ACCESS_READ),
    ("GET", "/api/v1/access/{connection_id}"): _human(ProjectAction.ACCESS_READ),
    ("PATCH", "/api/v1/access/{connection_id}"): _human(ProjectAction.ACCESS_MANAGE),
    ("DELETE", "/api/v1/access/{connection_id}"): _human(ProjectAction.ACCESS_MANAGE),
    ("PATCH", "/api/v1/access/{connection_id}/rename"): _human(ProjectAction.ACCESS_MANAGE),
    ("POST", "/api/v1/access/{connection_id}/regenerate-key"): _human(ProjectAction.CREDENTIAL_MANAGE),
    ("POST", "/api/v1/access/"): _human(ProjectAction.ACCESS_MANAGE),
})

for _method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
    PROJECT_ROUTE_AUTHORIZATION[(_method, "/api/v1/mcp/proxy")] = _runtime(
        "mcp.invoke"
    )
    PROJECT_ROUTE_AUTHORIZATION[(_method, "/api/v1/mcp/proxy/{path:path}")] = _runtime(
        "mcp.invoke"
    )

# Compatibility name for call sites that only consume Human action mappings.
PROJECT_ROUTE_ACTIONS = {
    key: contract.action
    for key, contract in PROJECT_ROUTE_AUTHORIZATION.items()
    if contract.plane is AuthorizationPlane.HUMAN_PROJECT
}
