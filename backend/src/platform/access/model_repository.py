"""Access domain persistence. Maps the existing JSON configuration to AccessSurface.

Physical schema remains unchanged during phase one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from src.infra.supabase.client import SupabaseClient
from src.platform.access.surface_repository import AccessSurfaceRepository
from src.platform.access.models import AccessSurface


class AccessModelRepository:
    TABLE = "access_surfaces"

    def __init__(self, supabase_client: Optional[SupabaseClient] = None):
        self._repo = AccessSurfaceRepository(supabase_client)

    # ── Reads ────────────────────────────────────────────────────────────

    def list_by_project(
        self,
        project_id: str,
        *,
        scope_id: Optional[str] = None,
        kind: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> list[AccessSurface]:
        rows = self._repo.list_surfaces_by_project(
            project_id,
            scope_id=scope_id,
            kind=kind,
        )
        if direction:
            rows = [row for row in rows if row.direction == direction]
        return rows

    def get(self, surface_id: str) -> Optional[AccessSurface]:
        return self._repo.get_surface(surface_id)

    def get_by_scope_kind(
        self, scope_id: str, kind: str,
    ) -> Optional[AccessSurface]:
        return self._repo.get_surface_by_scope_kind(scope_id, kind)

    def get_by_target_kind(
        self,
        project_id: str,
        scope_id: str | None,
        kind: str,
    ) -> Optional[AccessSurface]:
        row = self._repo.get_by_target_kind(project_id, scope_id, kind)
        return self._repo.get_surface(str(row["id"])) if row else None

    def count_third_party_for_scope(self, scope_id: str) -> int:
        return self._repo.count_bound_user_surfaces(scope_id)

    # ── Writes ───────────────────────────────────────────────────────────

    def insert(
        self,
        *,
        project_id: str,
        scope_id: Optional[str],
        kind: str,
        name: str,
        direction: str,
        config: dict,
        policy: dict,
        oauth_connection_id: Optional[int],
        trigger: dict,
        created_by: Optional[str],
    ) -> AccessSurface:
        merged_config = dict(config or {})
        merged_config["direction"] = direction
        merged_config["policy"] = policy or {}
        merged_config["trigger"] = trigger or {"type": "manual"}
        if oauth_connection_id is not None:
            merged_config["oauth_connection_id"] = oauth_connection_id
        row = self._repo.insert(
            project_id=project_id,
            scope_id=scope_id,
            kind=kind,
            name=name,
            config=merged_config,
            created_by=created_by,
        )
        return self._repo.get_surface(row["id"])

    def update(self, surface_id: str, patch: dict[str, Any]) -> Optional[AccessSurface]:
        if not patch:
            return self.get(surface_id)
        current = self._repo.get(surface_id)
        if current is None:
            return None

        update_data: dict[str, Any] = {}
        config = dict(current.get("config") or {})
        for key, value in patch.items():
            if key == "name":
                update_data["name"] = value
                config["name"] = value
            elif key == "status":
                update_data["status"] = value
            elif key == "config":
                config.update(value or {})
            elif key == "policy":
                config["policy"] = value or {}
            elif key == "trigger":
                config["trigger"] = value or {"type": "manual"}
            elif key == "direction":
                config["direction"] = value
            elif key == "error_message":
                config["error_message"] = value
            else:
                config[key] = value

        update_data["config"] = config
        updated = self._repo.update(surface_id, update_data)
        return self._repo.get_surface(updated["id"]) if updated else None

    def update_run_status(
        self,
        surface_id: str,
        *,
        status: str,
        last_run_at: Optional[datetime] = None,
        last_run_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self._repo.touch_run_status(
            surface_id,
            status=status,
            last_run_at=last_run_at,
            last_run_id=last_run_id,
            error_message=error_message,
        )

    def delete(self, surface_id: str) -> bool:
        return self._repo.delete(surface_id)
