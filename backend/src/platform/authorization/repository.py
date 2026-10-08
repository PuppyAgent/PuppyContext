"""Database facts for the canonical Project policy decision point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProjectAuthorizationFacts:
    project_id: str
    org_id: str
    visibility: str
    org_role: str | None
    project_role: str | None
    project_member_org_id: str | None


class AuthorizationRepository:
    """Read-only authorization facts.

    This repository is the only human authorization module allowed to read
    `org_members` and `project_members`. It returns raw facts; policy remains in
    `AuthorizationService` so SQL access and product decisions stay separate.
    """

    def __init__(self, supabase_client: Any | None = None):
        if supabase_client is None:
            from src.infra.supabase.dependencies import get_supabase_client

            supabase_client = get_supabase_client()
        self._client = supabase_client

    def load_project_facts(self, project_id: str, user_id: str) -> ProjectAuthorizationFacts | None:
        """Reuse the canonical joined facts query used by Agent admission."""
        data = (
            self._client.rpc(
                "authorization_project_facts", {"p_project": project_id, "p_user": user_id}
            )
            .execute()
            .data
        )
        return ProjectAuthorizationFacts(**data) if data is not None else None

    def load_project_facts_batch(
        self, project_ids: list[str], user_id: str
    ) -> dict[str, ProjectAuthorizationFacts]:
        """One set-based read per bounded batch, including absent memberships."""
        ids = list(dict.fromkeys(str(value) for value in project_ids if value))
        result: dict[str, ProjectAuthorizationFacts] = {}
        for offset in range(0, len(ids), 100):
            data = (
                self._client.rpc(
                    "authorization_project_facts_batch",
                    {"p_projects": ids[offset : offset + 100], "p_user": user_id},
                )
                .execute()
                .data
            )
            for row in data:
                facts = ProjectAuthorizationFacts(**row)
                result[facts.project_id] = facts
        return result


class ProjectMembershipRepository:
    """Administration port for the canonical Human membership fact table.

    Policy resolution remains in :class:`AuthorizationService`; Project
    settings code uses this port so raw membership storage never leaks into
    unrelated business services.
    """

    def __init__(self, supabase_client: Any | None = None):
        if supabase_client is None:
            from src.infra.supabase.dependencies import get_supabase_client

            supabase_client = get_supabase_client()
        self._client = supabase_client

    def list_by_project(self, project_id: str) -> list[dict[str, Any]]:
        try:
            response = (
                self._client.table("project_members")
                .select("*, profiles(email, display_name, avatar_url)")
                .eq("project_id", project_id)
                .order("created_at")
                .execute()
            )
        except Exception:
            response = (
                self._client.table("project_members")
                .select("*")
                .eq("project_id", project_id)
                .order("created_at")
                .execute()
            )
        return response.data or []

    def get(self, project_id: str, target_user_id: str) -> dict[str, Any] | None:
        response = (
            self._client.table("project_members")
            .select("id, org_id, project_id, user_id, role")
            .eq("project_id", project_id)
            .eq("user_id", target_user_id)
            .limit(1)
            .execute()
        )
        return self._row(response.data)

    def is_billable_organization_member(self, org_id: str, user_id: str) -> bool:
        """Use the database-owned capability policy for seat transitions."""

        data = (
            self._client.rpc(
                "is_billable_organization_member",
                {"p_org_id": org_id, "p_user_id": user_id},
            )
            .execute()
            .data
        )
        if isinstance(data, list):
            data = data[0] if data else False
        return bool(data)

    @staticmethod
    def _row(data: Any) -> dict[str, Any] | None:
        rows = data or []
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows if isinstance(rows, dict) else None

    def add(
        self,
        project_id: str,
        target_user_id: str,
        role: str,
        actor_user_id: str,
    ) -> dict[str, Any] | None:
        return self._row(
            self._client.rpc(
                "add_project_member_authorized",
                {
                    "p_project_id": project_id,
                    "p_target_user_id": target_user_id,
                    "p_role": role,
                    "p_actor_user_id": actor_user_id,
                },
            )
            .execute()
            .data
        )

    def update_role(
        self,
        project_id: str,
        target_user_id: str,
        role: str,
        actor_user_id: str,
    ) -> dict[str, Any] | None:
        return self._row(
            self._client.rpc(
                "update_project_member_role_authorized",
                {
                    "p_project_id": project_id,
                    "p_target_user_id": target_user_id,
                    "p_role": role,
                    "p_actor_user_id": actor_user_id,
                },
            )
            .execute()
            .data
        )

    def remove(self, project_id: str, target_user_id: str, actor_user_id: str) -> bool:
        data = (
            self._client.rpc(
                "remove_project_member_authorized",
                {
                    "p_project_id": project_id,
                    "p_target_user_id": target_user_id,
                    "p_actor_user_id": actor_user_id,
                },
            )
            .execute()
            .data
        )
        if isinstance(data, list):
            return bool(data and data[0])
        return bool(data)

    def join_with_share_token(self, share_token: str, user_id: str) -> dict[str, Any] | None:
        return self._row(
            self._client.rpc(
                "join_project_via_share_token",
                {"p_share_token": share_token, "p_user_id": user_id},
            )
            .execute()
            .data
        )
