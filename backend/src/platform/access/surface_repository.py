"""Supabase repository for workspace Access surfaces.

Access surfaces are target-bound ways to enter or operate on a workspace:
Git remote, CLI, agents, MCP endpoints, and sandboxes.
They are not Synchronize bindings or Database Import sources; those have independent stores.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.infra.supabase.client import SupabaseClient
from src.platform.repository_target.models import repository_target_from_storage
from src.repo.access_credentials import AccessCredentialRepository
from src.platform.access.models import AccessSurface
from src.repo.models import RepositoryScope, ResolvedAccessSurfaceCredential

ACCESS_SURFACE_KINDS = frozenset({
    "git_remote",
    "cli",
    "agent",
    "mcp",
    "sandbox",
})
ACCESS_SURFACE_KIND_LIST = sorted(ACCESS_SURFACE_KINDS)
STANDARD_TARGET_SURFACES = (
    ("git_remote", "Git Remote"),
    ("cli", "FS CLI"),
)


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _row_to_surface(row: dict[str, Any]) -> AccessSurface:
    config = row.get("config") or {}
    return AccessSurface(
        id=row["id"],
        target=repository_target_from_storage(
            str(row["project_id"]),
            str(row["scope_id"]) if row.get("scope_id") is not None else None,
        ),
        kind=row["kind"],
        name=row["name"],
        direction=config.get("direction") or (
            "bidirectional" if row["kind"] in {"git_remote", "cli"} else "inbound"
        ),
        config=config,
        policy=config.get("policy") or {},
        oauth_connection_id=None,
        trigger=config.get("trigger") or {"type": "manual"},
        status=row.get("status") or "active",
        last_run_at=_parse_dt(config.get("last_run_at")),
        last_run_id=config.get("last_run_id"),
        error_message=config.get("error_message"),
        created_by=row.get("created_by"),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class AccessSurfaceRepository:
    TABLE = "access_surfaces"
    CONNECTIONS = "synchronize_bindings"

    def __init__(self, supabase_client: Optional[SupabaseClient] = None):
        owner = supabase_client or SupabaseClient()
        self._client = owner if callable(getattr(owner, "table", None)) else owner.get_client()
        self._credentials = AccessCredentialRepository(self._client)

    def _project_org_id(self, project_id: str) -> str | None:
        resp = (
            self._client.table("projects")
            .select("org_id")
            .eq("id", project_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("org_id") if rows else None

    # ── Reads ────────────────────────────────────────────────────────────

    def list_by_project(
        self,
        project_id: str,
        *,
        scope_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        query = self._client.table(self.TABLE).select("*").eq("project_id", project_id)
        if scope_id:
            query = query.eq("scope_id", scope_id)
        if kind:
            if kind not in ACCESS_SURFACE_KINDS:
                return []
            query = query.eq("kind", kind)
        else:
            query = query.in_("kind", ACCESS_SURFACE_KIND_LIST)
        resp = query.order("created_at", desc=False).execute()
        return resp.data or []

    def list_by_projects(
        self,
        project_ids: list[str],
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Multi-project list used by tenant-authorized aggregate views."""
        if not project_ids:
            return []
        query = self._client.table(self.TABLE).select("*")
        query = (
            query.eq("project_id", project_ids[0])
            if len(project_ids) == 1
            else query.in_("project_id", project_ids)
        )
        if kind:
            if kind not in ACCESS_SURFACE_KINDS:
                return []
            query = query.eq("kind", kind)
        else:
            query = query.in_("kind", ACCESS_SURFACE_KIND_LIST)
        if status:
            query = query.eq("status", status)
        return query.order("created_at").execute().data or []

    def list_all(
        self, *, kind: Optional[str] = None, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        query = self._client.table(self.TABLE).select("*")
        if kind:
            if kind not in ACCESS_SURFACE_KINDS:
                return []
            query = query.eq("kind", kind)
        else:
            query = query.in_("kind", ACCESS_SURFACE_KIND_LIST)
        if status:
            query = query.eq("status", status)
        return query.order("created_at").execute().data or []

    def get_agent_with_project(self, surface_id: str) -> Optional[dict[str, Any]]:
        response = (
            self._client.table(self.TABLE)
            .select("*, project:project_id(created_by, org_id)")
            .eq("id", surface_id)
            .eq("kind", "agent")
            .single()
            .execute()
        )
        return response.data

    def list_tool_bindings(
        self,
        surface_id: str,
        *,
        enabled_only: bool = False,
        mcp_exposed_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Read canonical tool bindings for any access-surface kind."""

        query = (
            self._client.table("access_tools")
            .select("*")
            .eq("access_surface_id", surface_id)
        )
        if enabled_only:
            query = query.eq("enabled", True)
        if mcp_exposed_only:
            query = query.eq("mcp_exposed", True)
        return query.order("created_at").execute().data or []

    def count_by_projects_and_kinds(
        self, project_ids: list[str], kinds: list[str]
    ) -> dict[str, int]:
        if not project_ids:
            return {}
        valid_kinds = [kind for kind in kinds if kind in ACCESS_SURFACE_KINDS]
        if not valid_kinds:
            return {}
        rows = (
            self._client.table(self.TABLE)
            .select("project_id")
            .in_("project_id", project_ids)
            .in_("kind", valid_kinds)
            .execute()
        ).data or []
        counts: dict[str, int] = {}
        for row in rows:
            project_id = row["project_id"]
            counts[project_id] = counts.get(project_id, 0) + 1
        return counts

    def scope_rows_for(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Batch-load scope metadata for surface presentation/duplicate checks."""
        scope_ids = sorted({row.get("scope_id") for row in rows if row.get("scope_id")})
        if not scope_ids:
            return {}
        response = (
            self._client.table("repository_scopes").select("*").in_("id", scope_ids).execute()
        )
        return {row["id"]: row for row in (response.data or [])}

    def count_user_surfaces_by_project(self, project_id: str) -> int:
        response = (
            self._client.table(self.TABLE)
            .select("id", count="exact")
            .eq("project_id", project_id)
            .not_.in_("kind", ["git_remote", "cli"])
            .execute()
        )
        return response.count or 0

    def get(self, surface_id: str) -> Optional[dict[str, Any]]:
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("id", surface_id)
            .execute()
        )
        rows = resp.data or []
        row = rows[0] if rows else None
        if row and row.get("kind", row.get("provider")) not in ACCESS_SURFACE_KINDS:
            return None
        return row

    def get_by_scope_kind(self, scope_id: str, kind: str) -> Optional[dict[str, Any]]:
        rows = self.get_by_target_kind(None, scope_id, kind)
        return rows

    def get_by_target_kind(
        self,
        project_id: str | None,
        scope_id: str | None,
        kind: str,
    ) -> Optional[dict[str, Any]]:
        if kind not in ACCESS_SURFACE_KINDS:
            return None
        query = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("kind", kind)
        )
        if project_id is not None:
            query = query.eq("project_id", project_id)
        query = (
            query.is_("scope_id", "null")
            if scope_id is None
            else query.eq("scope_id", scope_id)
        )
        resp = query.order("created_at", desc=False).limit(1).execute()
        rows = resp.data or []
        return rows[0] if rows else None

    def get_by_config_key(self, kind: str, key: str, value: str) -> Optional[dict[str, Any]]:
        if kind not in ACCESS_SURFACE_KINDS:
            return None
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("kind", kind)
            .filter(f"config->>{key}", "eq", value)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None

    def list_surfaces_by_project(
        self,
        project_id: str,
        *,
        scope_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[AccessSurface]:
        return [
            _row_to_surface(row)
            for row in self.list_by_project(project_id, scope_id=scope_id, kind=kind)
        ]

    def get_surface(self, surface_id: str) -> Optional[AccessSurface]:
        row = self.get(surface_id)
        return _row_to_surface(row) if row else None

    def get_surface_by_scope_kind(self, scope_id: str, kind: str) -> Optional[AccessSurface]:
        row = self.get_by_scope_kind(scope_id, kind)
        return _row_to_surface(row) if row else None

    def resolve_scope_credential(
        self,
        raw_token: str,
    ) -> Optional[ResolvedAccessSurfaceCredential]:
        """Resolve a bearer token to one active, non-root CLI Surface.

        Credential identity and capability stay distinct from Scope geometry;
        the Scope repository joins the exact target in the next boundary.
        """

        credential = self._credentials.get_active_by_token(raw_token)
        if not credential:
            return None
        surface = self.get(credential["access_surface_id"])
        if (
            not surface
            or surface.get("kind") != "cli"
            or surface.get("status") != "active"
            or surface.get("scope_id") is None
            or not surface.get("org_id")
            or not credential.get("org_id")
            or str(surface.get("project_id")) != str(credential.get("project_id"))
            or str(surface.get("org_id")) != str(credential.get("org_id"))
        ):
            return None
        mode_facts = {
            str(credential.get("grant_mode") or "rw"),
            str((surface.get("config") or {}).get("mode") or "rw"),
        }
        if not mode_facts.issubset({"r", "rw"}):
            return None
        mode_ceiling = "r" if "r" in mode_facts else "rw"
        return ResolvedAccessSurfaceCredential(
            credential_id=str(credential["id"]),
            credential_type=str(credential["credential_type"]),
            access_surface_id=str(surface["id"]),
            project_id=str(surface["project_id"]),
            scope_id=str(surface["scope_id"]),
            mode_ceiling=mode_ceiling,
        )

    def store_scope_credential(
        self,
        *,
        scope_id: str,
        raw_token: str,
        created_by: str | None = None,
    ) -> bool:
        """Store/rotate the shared CLI/Git scope credential hash-only."""

        surface = self.get_by_scope_kind(scope_id, "cli")
        if not surface:
            return False
        self._credentials.store_bearer_token(
            access_surface_id=surface["id"],
            org_id=surface.get("org_id") or self._project_org_id(surface["project_id"]),
            project_id=surface["project_id"],
            raw_token=raw_token,
            created_by=created_by,
            revoke_existing=True,
        )
        return True

    def issue_scope_session_credential(
        self,
        *,
        scope_id: str,
        expires_at: datetime,
        created_by: str | None = None,
    ) -> Optional[str]:
        """Issue a non-disruptive, expiring token for an internal scope session."""

        surface = self.get_by_scope_kind(scope_id, "cli")
        if not surface:
            return None
        return self._credentials.issue_bearer_token(
            access_surface_id=surface["id"],
            org_id=surface.get("org_id") or self._project_org_id(surface["project_id"]),
            project_id=surface["project_id"],
            prefix="cli",
            created_by=created_by,
            revoke_existing=False,
            expires_at=expires_at,
        )

    def issue_git_session_credential(
        self,
        *,
        scope_id: str,
        expires_at: datetime,
        created_by: str | None = None,
    ) -> Optional[str]:
        """Issue a non-disruptive scoped Git token for an internal session."""

        surface = self.get_by_scope_kind(scope_id, "git_remote")
        if not surface or surface.get("status") != "active":
            return None
        scopes = (
            self._client.table("repository_scopes")
            .select("max_mode")
            .eq("id", scope_id)
            .eq("project_id", surface["project_id"])
            .limit(1)
            .execute()
        ).data or []
        if not scopes:
            return None
        return self._credentials.issue_git_http_token(
            access_surface_id=surface["id"],
            org_id=surface.get("org_id") or self._project_org_id(surface["project_id"]),
            project_id=surface["project_id"],
            grant_mode=str(scopes[0].get("max_mode") or "r"),
            prefix="git",
            created_by=created_by,
            revoke_existing=False,
            expires_at=expires_at,
        )

    # ── Writes ───────────────────────────────────────────────────────────

    def insert(
        self,
        *,
        project_id: str,
        scope_id: str | None,
        kind: str,
        name: str,
        config: Optional[dict[str, Any]] = None,
        status: str = "active",
        created_by: Optional[str] = None,
        principal_type: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if kind not in ACCESS_SURFACE_KINDS:
            raise ValueError(f"Unsupported access surface kind: {kind}")
        resp = (
            self._client.table(self.TABLE)
            .insert({
                "org_id": self._project_org_id(project_id),
                "project_id": project_id,
                "scope_id": scope_id,
                "kind": kind,
                "name": name,
                "status": status,
                "principal_type": principal_type,
                "principal_id": principal_id,
                "config": config or {},
                "created_by": created_by,
            })
            .execute()
        )
        return resp.data[0]

    def update(self, surface_id: str, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not patch:
            return self.get(surface_id)
        resp = (
            self._client.table(self.TABLE)
            .update(patch)
            .eq("id", surface_id)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None

    def delete(self, surface_id: str) -> bool:
        resp = self._client.table(self.TABLE).delete().eq("id", surface_id).execute()
        return bool(resp.data)

    def ensure_target_defaults(
        self,
        *,
        project_id: str,
        scope: RepositoryScope | None,
        created_by: Optional[str] = None,
    ) -> None:
        """Atomically enable the standard Git/CLI Surfaces for one target."""

        response = self._client.rpc(
            "ensure_repository_target_access_surfaces",
            {
                "p_project_id": project_id,
                "p_scope_id": scope.id if scope is not None else None,
                "p_created_by": created_by,
            },
        ).execute()
        rows = response.data or []
        enabled_kinds = {str(row.get("kind")) for row in rows}
        required_kinds = {kind for kind, _name in STANDARD_TARGET_SURFACES}
        if not required_kinds.issubset(enabled_kinds):
            raise RuntimeError("Repository target defaults were not enabled atomically")

    def count_bound_user_surfaces(self, scope_id: str) -> int:
        access_resp = (
            self._client.table(self.TABLE)
            .select("id", count="exact")
            .eq("scope_id", scope_id)
            .not_.in_("kind", ["git_remote", "cli"])
            .execute()
        )
        connection_resp = (
            self._client.table(self.CONNECTIONS)
            .select("id", count="exact")
            .eq("scope_id", scope_id)
            .execute()
        )
        return (access_resp.count or 0) + (connection_resp.count or 0)

    def touch_run_status(
        self,
        surface_id: str,
        *,
        status: str,
        last_run_at: Optional[datetime] = None,
        last_run_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        row = self.get(surface_id)
        if row is None:
            return
        config = dict(row.get("config") or {})
        if last_run_at is not None:
            config["last_run_at"] = last_run_at.isoformat()
        if last_run_id is not None:
            config["last_run_id"] = last_run_id
        config["error_message"] = error_message
        self.update(surface_id, {"status": status, "config": config})

    def touch_heartbeat(self, surface_id: str) -> None:
        row = self.get(surface_id)
        if row is None:
            return
        config = dict(row.get("config") or {})
        config["last_seen_at"] = datetime.now(timezone.utc).isoformat()
        self.update(surface_id, {"config": config})
