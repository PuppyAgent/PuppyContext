"""Supabase repository for non-root repository Scopes.

This is a thin wrapper around the Supabase client; all business rules
(canonicalization and product rules) live in scope_service.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Optional

from src.infra.supabase.client import SupabaseClient
from src.repo.models import RepositoryScope, ResolvedScopeCredential


def _row_to_scope(row: dict[str, Any]) -> RepositoryScope:
    return RepositoryScope(
        id=row["id"],
        project_id=row["project_id"],
        name=row.get("name") or row.get("path") or "Scope",
        path=row.get("path") or "",
        exclude=row.get("exclude") or [],
        max_mode=row.get("max_mode") or "rw",
        created_at=_parse_dt(row.get("created_at")),
        updated_at=_parse_dt(row.get("updated_at")),
    )


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class RepositoryScopeRepository:
    TABLE = "repository_scopes"

    def __init__(self, supabase_client: Optional[SupabaseClient] = None):
        owner = supabase_client or SupabaseClient()
        self._client = owner if callable(getattr(owner, "table", None)) else owner.get_client()

    # ── Reads ────────────────────────────────────────────────────────────

    def list_by_project(self, project_id: str) -> list[RepositoryScope]:
        """Return the Project's real Scopes ordered by path."""
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("project_id", project_id)
            .order("path", desc=False)
            .execute()
        )
        return [_row_to_scope(r) for r in (resp.data or [])]

    def list_paths_by_project(self, project_id: str) -> list[dict[str, str]]:
        response = (
            self._client.table(self.TABLE)
            .select("path")
            .eq("project_id", project_id)
            .execute()
        )
        return [{"path": row.get("path") or ""} for row in (response.data or [])]

    def get(self, scope_id: str) -> Optional[RepositoryScope]:
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("id", scope_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return _row_to_scope(rows[0]) if rows else None

    def resolve_access_key(
        self,
        access_key: str,
    ) -> Optional[ResolvedScopeCredential]:
        """Resolve one machine credential to its exact Scope target."""

        from src.platform.access.surface_repository import AccessSurfaceRepository

        credential = AccessSurfaceRepository(self._client).resolve_scope_credential(
            access_key
        )
        if credential is None:
            return None
        scope = self.get(credential.scope_id)
        if scope is None or scope.project_id != credential.project_id:
            return None
        if credential.mode_ceiling == "r" and scope.max_mode == "rw":
            scope = replace(scope, max_mode="r")
        return ResolvedScopeCredential(
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            access_surface_id=credential.access_surface_id,
            scope=scope,
        )

    def get_by_access_key(self, access_key: str) -> Optional[RepositoryScope]:
        resolved = self.resolve_access_key(access_key)
        return resolved.scope if resolved is not None else None

    def find_by_path_prefix(
        self, project_id: str, path: str,
    ) -> Optional[RepositoryScope]:
        """Return the scope whose path is the longest prefix of `path`.
        Used by path-to-scope inference.

        Example: scopes ['docs', 'docs/handbook']; path='docs/handbook/x.md'
        → returns the 'docs/handbook' scope."""
        all_scopes = self.list_by_project(project_id)
        target = (path or "").strip("/")
        # All scopes ordered shortest-to-longest path.
        candidates = sorted(all_scopes, key=lambda s: len(s.path))
        best: Optional[RepositoryScope] = None
        for s in candidates:
            sp = s.path
            if target == sp or target.startswith(sp + "/"):
                if best is None or len(s.path) > len(best.path):
                    best = s
        return best

    # ── Writes ───────────────────────────────────────────────────────────

    def insert(
        self,
        *,
        project_id: str,
        name: str,
        path: str,
        exclude: list[str],
        max_mode: str,
    ) -> RepositoryScope:
        """Insert a new path boundary without creating an Access Surface."""
        row: dict[str, Any] = {
            "project_id": project_id,
            "name": name,
            "path": path,
            "exclude": exclude,
            "max_mode": max_mode,
        }
        resp = self._client.table(self.TABLE).insert(row).execute()
        return _row_to_scope(resp.data[0])

    def update(
        self,
        scope_id: str,
        *,
        name: Optional[str] = None,
        exclude: Optional[list[str]] = None,
        max_mode: Optional[str] = None,
    ) -> Optional[RepositoryScope]:
        patch: dict[str, Any] = {}
        if name is not None:
            patch["name"] = name
        if exclude is not None:
            patch["exclude"] = exclude
        if max_mode is not None:
            patch["max_mode"] = max_mode
        if not patch:
            return self.get(scope_id)
        resp = (
            self._client.table(self.TABLE)
            .update(patch)
            .eq("id", scope_id)
            .execute()
        )
        rows = resp.data or []
        return _row_to_scope(rows[0]) if rows else None

    def delete(self, scope_id: str) -> bool:
        """Hard delete; the DB cascades resources bound to this exact Scope."""
        resp = (
            self._client.table(self.TABLE)
            .delete()
            .eq("id", scope_id)
            .execute()
        )
        return bool(resp.data)

    def update_path(self, scope_id: str, path: str) -> bool:
        """Infrastructure hook for a committed folder move.

        User-facing path changes still go through ScopeService; this narrow
        method keeps post-commit referential maintenance inside the repository.
        """
        response = (
            self._client.table(self.TABLE)
            .update({"path": path})
            .eq("id", scope_id)
            .execute()
        )
        return bool(response.data)
