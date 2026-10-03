"""Read-only repository over the ``context_activity_items`` view."""

from __future__ import annotations

from typing import Any

from src.infra.supabase.client import SupabaseClient
from src.platform.activity.schemas import ActivityItemResponse

# Kinds the view can emit; used to validate the optional filter.
ACTIVITY_KINDS = ("upload", "import", "synchronize_run")
TERMINAL_ACTIVITY_STATUSES = {
    "completed",
    "success",
    "failed",
    "cancelled",
    "canceled",
    "skipped",
    "conflict",
    "error",
}


def _is_active(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").lower()
    phase = str(row.get("phase") or "").lower()
    return (
        row.get("completed_at") is None
        and status not in TERMINAL_ACTIVITY_STATUSES
        and phase not in TERMINAL_ACTIVITY_STATUSES
    )


class ActivityRepository:
    """Reads the unified activity view. Never writes.

    The view is ``security_invoker = true`` but this repository uses the
    service-role client (RLS-bypassing), so callers MUST scope by project and
    enforce project access at the service layer — see ActivityService.
    """

    VIEW = "context_activity_items"

    def __init__(self, supabase_client: SupabaseClient | None = None):
        self.client = (supabase_client or SupabaseClient()).client

    def list_by_project(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[ActivityItemResponse]:
        query = (
            self.client.table(self.VIEW)
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if kind:
            query = query.eq("kind", kind)
        if active_only:
            # In-progress items have not finished yet — kind-agnostic, so we
            # don't have to track each table's status vocabulary.
            query = query.is_("completed_at", "null")
        rows: list[dict[str, Any]] = query.execute().data or []
        if active_only:
            rows = [row for row in rows if _is_active(row)]
        return [ActivityItemResponse.model_validate(row) for row in rows]
