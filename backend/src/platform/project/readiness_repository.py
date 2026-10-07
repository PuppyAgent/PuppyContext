"""One derived readiness snapshot over native HEAD and accepted Git operations."""

from __future__ import annotations

import base64
from typing import Any


class ProjectReadinessRepository:
    def __init__(self, supabase_client: Any | None = None):
        if supabase_client is None:
            from src.infra.supabase.dependencies import get_supabase_client

            supabase_client = get_supabase_client()
        self._client = supabase_client

    def load(self, project_id: str) -> dict[str, Any]:
        result = (
            self._client.rpc("get_native_project_readiness", {"p_project_id": project_id})
            .execute()
            .data
        )
        if not isinstance(result, dict):
            raise RuntimeError("native project readiness unavailable")
        name = base64.b64decode(result["default_branch_b64"], validate=True)
        branch = name.removeprefix(b"refs/heads/").decode("utf-8", "backslashreplace")
        return {**result, "default_branch": branch}
