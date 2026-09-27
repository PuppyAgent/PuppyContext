"""Resolve every MCP bearer key into the canonical scoped-FS runtime.

Authentication is deliberately performed here, once, against
``access_surface_credentials``.  Neither the MCP transport nor the individual
agent/endpoint connectors implement a second key lookup path.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from src.exceptions import ErrorCode
from src.infra.supabase.dependencies import get_supabase_client
from src.repo.access_credentials import AccessCredentialRepository
from src.platform.access.surface_repository import AccessSurfaceRepository
from src.repo.scope_repository import RepositoryScopeRepository

from .context import ScopedFsContext
from .policy import resolve_mcp_fs_allowed_tools


@dataclass(frozen=True)
class ResolvedMcpRuntime:
    context: ScopedFsContext
    surface_kind: str
    surface: dict


def _list_project_scopes(project_id: str) -> list[dict]:
    """All scope rows (path only) for the project — input for carved-exclude
    computation. Storage failures are authorization failures, never an empty
    topology, because dropping child exclusions would widen the runtime view."""
    try:
        return RepositoryScopeRepository(get_supabase_client()).list_paths_by_project(project_id)
    except Exception as exc:  # storage failures fail closed at the protocol boundary
        raise HTTPException(
            status_code=503,
            detail="Repository view could not be resolved",
            headers={
                "X-PuppyOne-Error-Code": str(
                    ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE.value
                )
            },
        ) from exc


def _merge_scope_excludes(user_excludes, scope_path: str, all_scopes: list[dict]) -> list[str]:
    """Merge user-configured excludes with carved child-scope paths (GAP-4).

    An MCP Scope must hide its declared child Scopes — the same isolation
    the git/CLI admission path applies via ``compute_carved_excludes``. Without
    this an MCP key bound to a parent scope could ``ls``/``cat``/``grep`` into its
    child Scope subtrees. The Project-root projection (``scope_path == ""``)
    intentionally carves nothing.
    """
    from src.version_engine.admission.repo_facade import compute_carved_excludes
    carved = list(compute_carved_excludes(scope_path, all_scopes))
    return list(dict.fromkeys(list(user_excludes) + carved))


def _resolve_surface(key: str) -> tuple[dict, dict]:
    """Return the authenticated surface and its provider-specific policy."""

    sb = get_supabase_client()
    credential = AccessCredentialRepository(sb).get_active_by_token(key)
    if not credential:
        raise HTTPException(status_code=404, detail="MCP access surface not found")

    surface = AccessSurfaceRepository(sb).get(credential["access_surface_id"])
    if not surface or surface.get("kind") not in {"agent", "mcp"}:
        raise HTTPException(status_code=404, detail="MCP access surface not found")
    if surface.get("status") != "active":
        raise HTTPException(status_code=403, detail="MCP access surface is not active")

    policy: dict = {}
    if surface.get("kind") == "mcp":
        policy_resp = (
            sb.table("access_surface_policies")
            .select("*")
            .eq("access_surface_id", surface["id"])
            .limit(1)
            .execute()
        )
        policy_rows = policy_resp.data or []
        policy = policy_rows[0] if policy_rows else {}
    return surface, policy


def resolve_mcp_runtime(api_key: str) -> ResolvedMcpRuntime:
    key = (api_key or "").strip()
    if not key.startswith("mcp_"):
        raise HTTPException(status_code=401, detail="Invalid MCP API key")

    surface, policy = _resolve_surface(key)

    scope_id = surface.get("scope_id")
    if not scope_id:
        raise HTTPException(status_code=403, detail="MCP access surface is not bound to a scope")

    sb = get_supabase_client()
    scope_model = RepositoryScopeRepository(sb).get(scope_id)
    if not scope_model:
        raise HTTPException(status_code=403, detail="MCP endpoint scope not found")
    scope = {
        "id": scope_model.id,
        "project_id": scope_model.project_id,
        "name": scope_model.name,
        "path": scope_model.path,
        "exclude": scope_model.exclude,
        "mode": scope_model.max_mode,
    }

    fs_policy = policy.get("fs_policy") or {}
    accesses = fs_policy.get("accesses") or []
    # Agent permissions are exactly its bound repo scope. MCP endpoints may
    # further reduce that upper bound through their access policy.
    access_writable = surface.get("kind") == "agent" or any(
        not bool(access.get("readonly", True)) for access in accesses
    )
    mode = "rw" if scope.get("mode") == "rw" and access_writable else "ro"
    allowed_tools = resolve_mcp_fs_allowed_tools(
        policy.get("tools_policy") if surface.get("kind") == "mcp" else None,
        writable=mode == "rw",
    )

    user_id = surface.get("created_by") or ""
    if not user_id:
        project_resp = (
            sb.table("projects")
            .select("created_by")
            .eq("id", surface["project_id"])
            .limit(1)
            .execute()
        )
        project_rows = project_resp.data or []
        user_id = project_rows[0].get("created_by") if project_rows else ""

    scope_path = (scope.get("path") or "").strip("/")
    exclude = _merge_scope_excludes(
        scope.get("exclude") or [],
        scope_path,
        _list_project_scopes(surface["project_id"]),
    )

    context = ScopedFsContext(
        api_key=key,
        endpoint_id=surface["id"],
        endpoint_name=surface.get("name") or "MCP Access Surface",
        project_id=surface["project_id"],
        user_id=user_id or "",
        scope_id=scope["id"],
        scope_path=scope_path,
        mode=mode,
        exclude=exclude,
        allowed_tools=allowed_tools,
    )
    return ResolvedMcpRuntime(
        context=context,
        surface_kind=surface["kind"],
        surface=surface,
    )


def resolve_mcp_scoped_fs_context(api_key: str) -> ScopedFsContext:
    """Compatibility facade for scoped-FS callers that need only context."""

    return resolve_mcp_runtime(api_key).context
