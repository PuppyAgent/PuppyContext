"""Workspace Access operations; public_router alone owns resource HTTP routes.

Access manages target-bound ways to enter or operate on a workspace:
Git remote, FS CLI, agents, MCP endpoints, and sandboxes.
External source relationships belong to Synchronize, not Access.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from src.common_schemas import ApiResponse
from src.config import settings
from src.exceptions import AppException, ErrorCode, NotFoundException
from src.infra.supabase.client import SupabaseClient
from src.platform.access.surface_repository import AccessSurfaceRepository
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.entitlements.dependencies import get_entitlement_service
from src.platform.entitlements.service import EntitlementService
from src.platform.organization.dependencies import resolve_org_ids
from src.platform.repository_target.models import ProjectRootTarget, ScopeTarget
from src.platform.repository_target.schemas import (
    RepositoryTargetSchema,
    repository_target_schema,
)
from src.repo.access_credentials import AccessCredentialRepository

# ── Schemas ─────────────────────────────────────────────────


class ConnectionOut(BaseModel):
    id: str
    project_id: str
    target: RepositoryTargetSchema
    provider: str
    name: str | None = None
    path: str | None = None
    node_name: str | None = None
    direction: str | None = None
    status: str = "active"
    access_key: str | None = None
    has_key: bool = False
    key_last4: str | None = None
    gateway_id: str | None = None
    trigger: dict | None = None
    last_synced_at: str | None = None
    error_message: str | None = None
    config: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ConnectionUpdate(BaseModel):
    status: str | None = None
    trigger: dict | None = None
    config: dict | None = None


# ── Helpers ─────────────────────────────────────────────────


def _get_client():
    return SupabaseClient().client


def _normalize_scope_path(path: str | None) -> str:
    value = (path or "").strip()
    while value.startswith("/"):
        value = value[1:]
    while value.endswith("/"):
        value = value[:-1]
    while "//" in value:
        value = value.replace("//", "/")
    return value


# Config keys that hold machine credentials / secrets. These must never be
# returned by ordinary list/detail/mutation responses. Raw values are only
# meaningful in the one-time create/regenerate issuance responses.
_SECRET_CONFIG_KEYS = {
    "access_key",
    "mcp_api_key",
    "api_key",
    "secret",
    "client_secret",
    "token",
    "bearer_token",
    "refresh_token",
    "access_token",
    "password",
    "private_key",
    "credential",
    "credentials",
}

_SECRET_CONFIG_KEY_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_secret",
    "_password",
    "_private_key",
    "_bearer_token",
    "_access_token",
    "_refresh_token",
    "_credential",
    "_credentials",
)


def _normalise_config_key(key: Any) -> str:
    """Return a conservative snake-case-ish representation for secret checks."""
    import re

    value = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _is_secret_config_key(key: Any) -> bool:
    normalised = _normalise_config_key(key)
    return normalised in _SECRET_CONFIG_KEYS or normalised.endswith(_SECRET_CONFIG_KEY_SUFFIXES)


def _redact_config(value: Any) -> Any:
    """Recursively remove credential-bearing fields from response config.

    Access surface config is intentionally provider-extensible, so a shallow
    blacklist is unsafe: a provider can nest credentials under OAuth/session
    metadata and ordinary read or mutation responses would echo them. Keep the
    non-secret shape for existing clients, but fail closed for known credential
    names at every depth (including camelCase variants and provider prefixes).
    """
    if isinstance(value, dict):
        return {
            key: _redact_config(child)
            for key, child in value.items()
            if not _is_secret_config_key(key)
        }
    if isinstance(value, list):
        return [_redact_config(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_config(child) for child in value)
    return value


def _contains_secret_config_key(value: Any) -> bool:
    """Reject attempts to smuggle credentials through the metadata endpoint."""
    if isinstance(value, dict):
        return any(
            _is_secret_config_key(key) or _contains_secret_config_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_secret_config_key(child) for child in value)
    return False


def _created_or_updated(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _enrich(rows: list[dict], sb_client) -> list[ConnectionOut]:
    """Resolve node names from paths and extract config.name for display.

    Auto-disambiguates duplicate display names by appending path or a counter,
    so users can tell apart multiple connections of the same provider type.

    This ordinary response serializer is credential-free by construction.
    Raw credentials are issued only by the explicit create/regenerate response
    models; callers cannot opt this serializer into returning a bearer token.
    """
    scopes = AccessSurfaceRepository(sb_client).scope_rows_for(rows)
    from src.repo.access_credentials import AccessCredentialRepository

    credentials = AccessCredentialRepository(sb_client).list_active_by_surface(
        [row["id"] for row in rows]
    )
    # First pass: build raw entries
    entries = []
    for r in rows:
        cfg = r.get("config") or {}
        scope = scopes.get(r.get("scope_id"))
        kind = r.get("kind", r.get("provider", ""))
        base_name = r.get("name") or cfg.get("name") or cfg.get("sync_url") or kind
        node_path = _normalize_scope_path((scope or {}).get("path"))
        node_name = node_path.rsplit("/", 1)[-1] if node_path else None
        entries.append(
            {
                "row": r,
                "cfg": cfg,
                "scope": scope,
                "kind": kind,
                "base_name": base_name,
                "node_path": node_path,
                "node_name": node_name,
            }
        )

    # Second pass: detect duplicates and disambiguate names
    from collections import Counter

    name_counts = Counter(e["base_name"] for e in entries)
    name_seen: dict[str, int] = {}

    out: list[ConnectionOut] = []
    for e in entries:
        r = e["row"]
        cfg = e["cfg"]
        base_name = e["base_name"]
        node_path = e["node_path"]

        if name_counts[base_name] > 1:
            # Disambiguate: prefer path suffix, fall back to counter
            if node_path:
                display_path = node_path.strip("/")
                disambig = display_path if display_path else "root"
                name = f"{base_name} ({disambig})"
            else:
                name_seen[base_name] = name_seen.get(base_name, 0) + 1
                name = f"{base_name} #{name_seen[base_name]}"
        else:
            name = base_name

        credential = credentials.get(r["id"])
        has_key = credential is not None
        key_last4 = credential.get("key_last4") if credential else None

        out.append(
            ConnectionOut(
                id=r["id"],
                project_id=r["project_id"],
                target=repository_target_schema(
                    ScopeTarget(
                        project_id=r["project_id"],
                        scope_id=str(r["scope_id"]),
                    )
                    if r.get("scope_id") is not None
                    else ProjectRootTarget(project_id=r["project_id"])
                ),
                provider=e["kind"],
                name=name,
                path=node_path or None,
                node_name=e["node_name"],
                direction=cfg.get("direction"),
                status=r.get("status", "active"),
                access_key=None,
                has_key=has_key,
                key_last4=key_last4,
                gateway_id=(e["cfg"].get("gateway_id") if isinstance(e["cfg"], dict) else None),
                trigger=_redact_config(cfg.get("trigger")),
                last_synced_at=_created_or_updated(
                    cfg.get("last_seen_at") or cfg.get("last_run_at")
                ),
                error_message=cfg.get("error_message"),
                config=_redact_config(e["cfg"]),
                created_at=_created_or_updated(r.get("created_at")),
                updated_at=_created_or_updated(r.get("updated_at")),
            )
        )
    return out


def _get_user_project_ids(
    sb_client,
    org_ids: list[str],
    authorization: AuthorizationService,
    user_id: str,
) -> list[str]:
    """Discover tenant candidates, then filter them through ProjectGrant."""
    if not org_ids:
        return []
    resp = sb_client.table("projects").select("id").in_("org_id", org_ids).execute()
    return authorization.accessible_project_ids(
        [str(row["id"]) for row in (resp.data or [])], user_id
    )


def _require_connection_project_access(
    authorization: AuthorizationService,
    project_id: str,
    user_id: str,
    action: ProjectAction,
) -> None:
    authorization.authorize(project_id, user_id, action)


# ── Endpoints ───────────────────────────────────────────────


def list_connections(
    project_id: str | None = Query(None),
    provider: str | None = Query(None),
    connection_status: str | None = Query(None, alias="status"),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    sb = _get_client()
    org_ids = resolve_org_ids(None, current_user.user_id)
    allowed_project_ids = _get_user_project_ids(
        sb, org_ids, authorization, current_user.user_id
    )

    if project_id:
        # project_id is client-controlled input and must be re-authorized against
        # the caller's own membership set — otherwise this list endpoint is an IDOR
        # into any tenant's access points. Mirror get_connection's non-leak
        # convention: return empty rather than confirming another tenant's project.
        if project_id not in allowed_project_ids:
            return ApiResponse.success(data=[], message="No access connections")
        project_ids = [project_id]
    else:
        project_ids = allowed_project_ids

    if not project_ids:
        return ApiResponse.success(data=[], message="No access connections")

    rows = AccessSurfaceRepository(sb).list_by_projects(
        project_ids, kind=provider, status=connection_status
    )
    # List views never emit raw credentials (IDOR would otherwise leak usable keys).
    return ApiResponse.success(data=_enrich(rows, sb), message="Access connections listed")


def get_connection(
    connection_id: str = Path(...),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    sb = _get_client()
    row = AccessSurfaceRepository(sb).get(connection_id)
    if not row:
        raise NotFoundException("Access connection not found", code=ErrorCode.NOT_FOUND)
    _require_connection_project_access(
        authorization, row["project_id"], current_user.user_id, ProjectAction.ACCESS_READ
    )

    # Detail views must not echo raw credentials (access_key / config secrets).
    return ApiResponse.success(data=_enrich([row], sb)[0], message="Access connection found")


async def update_connection(
    payload: ConnectionUpdate,
    connection_id: str = Path(...),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    sb = _get_client()

    surfaces = AccessSurfaceRepository(sb)
    row = surfaces.get(connection_id)
    if not row:
        raise NotFoundException("Access connection not found", code=ErrorCode.NOT_FOUND)
    _require_connection_project_access(
        authorization,
        row["project_id"],
        current_user.user_id,
        ProjectAction.ACCESS_MANAGE,
    )

    fields: dict[str, Any] = {}
    if payload.status is not None:
        fields["status"] = payload.status
    if payload.config is not None:
        if _contains_secret_config_key(payload.config):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Credentials cannot be updated through access metadata; "
                    "use the dedicated create or regenerate-key flow"
                ),
            )
        cfg = dict(row.get("config") or {})
        cfg.update(payload.config)
        fields["config"] = cfg
    if payload.trigger is not None:
        if _contains_secret_config_key(payload.trigger):
            raise HTTPException(400, "Credentials cannot be updated through access metadata; use explicit credential issuance")
        cfg = dict(fields.get("config") or row.get("config") or {})
        cfg["trigger"] = payload.trigger
        fields["config"] = cfg

    updated = surfaces.update(connection_id, fields)
    return ApiResponse.success(
        data=_enrich([updated], sb)[0], message="Access connection updated"
    )


async def delete_connection(
    connection_id: str = Path(...),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    sb = _get_client()

    surfaces = AccessSurfaceRepository(sb)
    row = surfaces.get(connection_id)
    if not row:
        raise NotFoundException("Access connection not found", code=ErrorCode.NOT_FOUND)

    _require_connection_project_access(
        authorization,
        row["project_id"],
        current_user.user_id,
        ProjectAction.ACCESS_MANAGE,
    )

    if row.get("kind") in {"git_remote", "cli"}:
        raise HTTPException(status_code=400, detail="Built-in access surfaces cannot be deleted")
    surfaces.delete(connection_id)
    return ApiResponse.success(message="Access connection deleted")


def rename_connection(
    connection_id: str = Path(...),
    body: dict = Body(...),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    """Update only the display name stored in config.name."""
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name must not be empty")

    sb = _get_client()
    surfaces = AccessSurfaceRepository(sb)
    row = surfaces.get(connection_id)
    if not row:
        raise NotFoundException("Access connection not found", code=ErrorCode.NOT_FOUND)
    _require_connection_project_access(
        authorization,
        row["project_id"],
        current_user.user_id,
        ProjectAction.ACCESS_MANAGE,
    )

    cfg = dict(row.get("config") or {})
    cfg["name"] = new_name
    updated = surfaces.update(connection_id, {"name": new_name, "config": cfg})
    return ApiResponse.success(
        data=_enrich([updated], sb)[0], message="Access connection renamed"
    )


def regenerate_key(
    connection_id: str = Path(...),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    sb = _get_client()

    row = AccessSurfaceRepository(sb).get(connection_id)
    if not row:
        raise NotFoundException("Access connection not found", code=ErrorCode.NOT_FOUND)
    _require_connection_project_access(
        authorization,
        row["project_id"],
        current_user.user_id,
        ProjectAction.CREDENTIAL_MANAGE,
    )

    provider = row.get("kind", row.get("provider", ""))
    if provider == "git_remote":
        raise AppException(
            code=ErrorCode.CLIENT_UPGRADE_REQUIRED,
            status_code=status.HTTP_410_GONE,
            message=(
                "Git credentials must be client-generated through "
                "POST /api/v1/projects/{project_id}/git-credentials"
            ),
            details={"required_repository_contract": 2},
        )
    if provider == "cli":
        new_key = AccessCredentialRepository(sb).issue_bearer_token(
            access_surface_id=row["id"],
            org_id=row["org_id"],
            project_id=row["project_id"],
            prefix="cli",
            created_by=current_user.user_id,
        )
        target = (
            ScopeTarget(project_id=row["project_id"], scope_id=row["scope_id"])
            if row.get("scope_id") is not None
            else ProjectRootTarget(project_id=row["project_id"])
        )
        return ApiResponse.success(
            data={
                "credential": new_key,
                "target": repository_target_schema(target).model_dump(),
            },
            message="Key regenerated",
        )
    if provider == "sandbox":
        from src.platform.access.adapters.sandbox_endpoint.repository import (
            SandboxEndpointRepository,
        )

        endpoint = SandboxEndpointRepository(sb).regenerate_access_key(connection_id)
        if not endpoint or not endpoint.get("access_key"):
            raise NotFoundException("Sandbox endpoint not found", code=ErrorCode.NOT_FOUND)
        return ApiResponse.success(
            data={"access_key": endpoint["access_key"]}, message="Key regenerated"
        )
    elif provider == "mcp":
        from src.platform.access.adapters.mcp_endpoint.repository import McpEndpointRepository
        from src.platform.access.adapters.mcp_endpoint.service import McpEndpointService

        endpoint = McpEndpointService(repository=McpEndpointRepository()).regenerate_key(
            connection_id
        )
        if not endpoint or not endpoint.get("api_key"):
            raise NotFoundException("MCP endpoint not found", code=ErrorCode.NOT_FOUND)
        return ApiResponse.success(
            data={
                "access_key": endpoint["api_key"],
                "access_key_hint": endpoint.get("api_key_hint"),
            },
            message="Key regenerated",
        )
    elif provider == "agent":
        from src.platform.access.adapters.agent.config.repository import AgentRepository

        new_key = AgentRepository(sb).regenerate_mcp_api_key(connection_id)
        if not new_key:
            raise NotFoundException("Agent not found", code=ErrorCode.NOT_FOUND)
        return ApiResponse.success(
            data={"access_key": new_key}, message="Key regenerated"
        )
    raise HTTPException(status_code=400, detail="This access surface has no bearer key")


# ── Connection Types (unified) ─────────────────────────────


def list_connection_types():
    """
    Returns available workspace Access surface types.
    """
    access_types = [
        {
            "provider": "agent",
            "display_name": "Chat Agent",
            "description": "Interactive AI assistant with data access",
            "auth": "none",
            "creation_mode": "direct",
            "category": "access",
            "icon": "bot",
        },
        {
            "provider": "mcp",
            "display_name": "MCP Server",
            "description": "Model Context Protocol endpoint",
            "auth": "none",
            "creation_mode": "direct",
            "category": "access",
            "icon": "plug",
        },
        {
            "provider": "sandbox",
            "display_name": "Sandbox",
            "description": "Isolated script execution environment",
            "auth": "none",
            "creation_mode": "direct",
            "category": "access",
            "icon": "box",
        },
    ]

    return ApiResponse.success(data=access_types)


# ── Unified Create ─────────────────────────────────────────


class UnifiedConnectionCreate(BaseModel):
    """
    Single request schema for creating ANY access type.
    The `provider` field determines which service handles creation.
    """

    project_id: str = Field(..., description="Project ID")
    target: RepositoryTargetSchema | None = Field(
        None,
        description="Required for direct Git/CLI access; ignored by other providers",
    )
    provider: str = Field(..., description="Access type: gmail, github, agent, mcp, sandbox, ...")
    name: str | None = Field(None, description="Display name")
    path: str | None = Field(None, description="Target version path")
    config: dict = Field(default_factory=dict, description="Provider-specific configuration")
    gateway_id: str | None = Field(
        None, description="Gateway ID (required for datasource providers)"
    )
    direction: str | None = Field(None, description="Sync direction (datasource only)")
    trigger: dict | None = Field(None, description="Trigger config (datasource/agent)")
    credentials_ref: str | None = Field(
        None, description="OAuth credentials reference (datasource)"
    )
    sync_mode: str | None = Field(
        None, description="Sync mode: manual, scheduled, realtime (datasource)"
    )
    conflict_strategy: str | None = Field(None, description="Conflict strategy (datasource)")
    accesses: list[dict] | None = Field(None, description="Node access bindings (agent/mcp)")
    tools_config: list[dict] | None = Field(None, description="Tool bindings (mcp)")


class UnifiedConnectionOut(BaseModel):
    id: str
    project_id: str
    provider: str
    name: str | None = None
    status: str = "active"
    gateway_id: str | None = None
    target: RepositoryTargetSchema | None = None
    git_url: str | None = None
    git_username: str | None = None
    git_credential: str | None = None
    cli_access_key: str | None = None
    mcp_api_key: str | None = Field(
        None,
        description="One-time MCP bearer credential returned only by creation",
    )
    mcp_server_url: str | None = Field(
        None,
        description="Canonical public MCP proxy URL; authenticate with Authorization: Bearer",
    )


def _public_mcp_server_url() -> str | None:
    base = (settings.PUBLIC_URL or "").rstrip("/")
    return f"{base}/api/v1/mcp/proxy" if base else None


def _create_agent(payload: UnifiedConnectionCreate) -> UnifiedConnectionOut:
    from src.platform.access.adapters.agent.config.repository import AgentRepository
    from src.platform.access.adapters.agent.config.schemas import AgentBashCreate
    from src.platform.access.adapters.agent.config.service import AgentConfigService

    service = AgentConfigService(repository=AgentRepository())

    bash_accesses = []
    if payload.accesses:
        bash_accesses = [
            AgentBashCreate(
                path=a["path"],
                readonly=a.get("readonly", a.get("terminal_readonly", True)),
            )
            for a in payload.accesses
        ]

    cfg = payload.config
    agent = service.create_agent(
        project_id=payload.project_id,
        name=payload.name or cfg.get("name", "Chat Agent"),
        icon=cfg.get("icon", "✨"),
        type=cfg.get("type", "chat"),
        description=cfg.get("description"),
        bash_accesses=bash_accesses,
        trigger_type=cfg.get("trigger_type", "manual"),
        trigger_config=cfg.get("trigger_config"),
        task_content=cfg.get("task_content"),
        task_path=cfg.get("task_path"),
        external_config=cfg.get("external_config"),
    )
    return UnifiedConnectionOut(
        id=agent.id,
        project_id=payload.project_id,
        provider="agent",
        name=agent.name,
        status="active",
        mcp_api_key=agent.mcp_api_key,
        mcp_server_url=_public_mcp_server_url(),
    )


def _create_mcp(
    payload: UnifiedConnectionCreate, *, created_by: str | None = None
) -> UnifiedConnectionOut:
    from src.platform.access.adapters.mcp_endpoint.repository import McpEndpointRepository
    from src.platform.access.adapters.mcp_endpoint.schemas import McpAccessItem, McpToolItem
    from src.platform.access.adapters.mcp_endpoint.service import McpEndpointService

    service = McpEndpointService(repository=McpEndpointRepository())

    accesses = [McpAccessItem(**a) for a in (payload.accesses or [])]
    tools = [McpToolItem(**t) for t in (payload.tools_config or [])]

    row = service.create_endpoint(
        project_id=payload.project_id,
        name=payload.name or payload.config.get("name", "MCP Endpoint"),
        path=payload.path,
        description=payload.config.get("description"),
        accesses=accesses,
        tools_config=tools,
        created_by=created_by,
    )
    return UnifiedConnectionOut(
        id=row["id"],
        project_id=row["project_id"],
        provider="mcp",
        name=row["name"],
        status=row["status"],
        mcp_api_key=row.get("api_key"),
        mcp_server_url=_public_mcp_server_url(),
    )


def _create_sandbox(payload: UnifiedConnectionCreate) -> UnifiedConnectionOut:
    from src.platform.access.adapters.sandbox_endpoint.repository import SandboxEndpointRepository
    from src.platform.access.adapters.sandbox_endpoint.schemas import (
        SandboxMountItem,
        SandboxResourceLimits,
    )
    from src.platform.access.adapters.sandbox_endpoint.service import SandboxEndpointService

    service = SandboxEndpointService(repository=SandboxEndpointRepository())

    cfg = payload.config
    mounts = [SandboxMountItem(**m) for m in cfg.get("mounts", [])] or None
    resource_limits = (
        SandboxResourceLimits(**cfg["resource_limits"]) if cfg.get("resource_limits") else None
    )

    row = service.create_endpoint(
        project_id=payload.project_id,
        name=payload.name or cfg.get("name", "Sandbox"),
        path=payload.path,
        description=cfg.get("description"),
        mounts=mounts,
        runtime=cfg.get("runtime", "alpine"),
        timeout_seconds=cfg.get("timeout_seconds", 30),
        resource_limits=resource_limits,
    )
    return UnifiedConnectionOut(
        id=row["id"],
        project_id=row["project_id"],
        provider="sandbox",
        name=row["name"],
        status=row["status"],
    )


async def create_connection(
    payload: UnifiedConnectionCreate,
    current_user: CurrentUser = Depends(get_current_user),
    entitlement_service: EntitlementService = Depends(get_entitlement_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    """
    Unified entry point for creating Access surfaces.
    Routes to the appropriate service based on `provider`.

    - agent: creates a chat agent
    - mcp: creates an MCP endpoint
    - sandbox: creates a sandbox endpoint
    The removed ``direct`` provider returns 410 so legacy clients cannot cause
    the server to generate a plaintext human Git credential. Human Git
    credentials are accepted only by the client-generated, idempotent Project
    credential endpoint.
    """
    from src.platform.project.repository import ProjectRepositorySupabase

    provider = payload.provider.lower()
    if provider == "direct":
        raise AppException(
            code=ErrorCode.CLIENT_UPGRADE_REQUIRED,
            status_code=status.HTTP_410_GONE,
            message=(
                "Direct Git credential creation was removed; use "
                "POST /api/v1/projects/{project_id}/git-credentials"
            ),
            details={
                "code": "legacy_direct_access_removed",
                "required_repository_contract": 2,
            },
        )
    if provider not in {"agent", "mcp", "sandbox"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown access provider: {provider}. Use Integration APIs "
                "for external datasource connections."
            ),
        )

    action_by_provider = {
        "agent": ProjectAction.AGENT_MANAGE,
        "mcp": ProjectAction.MCP_MANAGE,
        "sandbox": ProjectAction.SANDBOX_MANAGE,
    }
    authorization.authorize(
        payload.project_id,
        current_user.user_id,
        action_by_provider[provider],
    )
    project_repo = ProjectRepositorySupabase()
    project = project_repo.get_by_id(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # ── Duplicate detection ────────────────────────────────────────
    # Block creation if an identical access surface already exists.
    # "Identical" = same project + provider + path + key config fields.
    surfaces = AccessSurfaceRepository()
    existing = surfaces.list_by_project(payload.project_id, kind=provider)
    existing_scopes = surfaces.scope_rows_for(existing)

    for ex in existing:
        ex_scope = existing_scopes.get(ex.get("scope_id")) or {}
        ex_path = _normalize_scope_path(ex_scope.get("path"))
        new_path = _normalize_scope_path(payload.path)
        if ex_path != new_path:
            continue
        # Same path — compare key config fields per provider
        is_dup = False
        if provider in ("agent", "mcp", "sandbox"):
            is_dup = True
        if is_dup:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_access_surface",
                    "message": "An access surface with the same configuration already exists.",
                    "existing_id": ex["id"],
                },
            )

    entitlement_service.require_allowed(
        project.org_id,
        "access_surface_kinds",
        provider,
    )
    entitlement_service.require_feature(project.org_id, f"access_surface.{provider}")
    entitlement_service.require_capacity(
        project.org_id,
        "access_surfaces.max_per_project",
        current_count=surfaces.count_user_surfaces_by_project(payload.project_id),
    )

    try:
        if provider == "agent":
            result = _create_agent(payload)
        elif provider == "mcp":
            result = _create_mcp(payload, created_by=current_user.user_id)
        elif provider == "sandbox":
            result = _create_sandbox(payload)
    except HTTPException:
        raise
    except Exception as e:
        from src.utils.logger import log_error

        log_error(f"Failed to create {provider} access surface: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to create {provider} access surface: {e}"
        ) from e

    return ApiResponse.success(data=result, message=f"{provider} access surface created")
