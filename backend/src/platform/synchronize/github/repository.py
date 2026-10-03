"""Repository for ``synchronize_github_bindings`` and ``synchronize_github_logs`` tables.

Thin async wrapper over the Supabase client — keeps SQL/JSON shape
out of the service layer. Mirrors the pattern used by
``src/platform/access/model_repository.py``.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from src.infra.supabase.client import SupabaseClient
from src.version_engine.infrastructure.supabase.db_names import GITHUB_SYNC_VERSION_COLUMN


class GithubSyncRepository:
    """CRUD for ``public.synchronize_github_bindings``."""

    TABLE = "synchronize_github_bindings"

    def __init__(self, client: Optional[SupabaseClient] = None):
        self._sb = (client or SupabaseClient()).client

    # ── reads ─────────────────────────────────────

    async def get_by_project(self, project_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._get_by_project_sync, project_id)

    def _get_by_project_sync(self, project_id: str) -> Optional[dict]:
        resp = (
            self._sb.table(self.TABLE)
            .select("*")
            .eq("project_id", project_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None

    async def get_by_id(self, binding_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._get_by_id_sync, binding_id)

    def _get_by_id_sync(self, binding_id: str) -> Optional[dict]:
        resp = (
            self._sb.table(self.TABLE)
            .select("*")
            .eq("id", binding_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None

    async def find_by_repo(self, owner: str, name: str) -> list[dict]:
        """Webhook reverse-lookup: GitHub identifies the source repo
        by owner+name; we have to find which projects bind to it."""
        return await asyncio.to_thread(self._find_by_repo_sync, owner, name)

    def _find_by_repo_sync(self, owner: str, name: str) -> list[dict]:
        resp = (
            self._sb.table(self.TABLE)
            .select("*")
            .eq("github_repo_owner", owner)
            .eq("github_repo_name", name)
            .execute()
        )
        return resp.data or []

    # ── writes ────────────────────────────────────

    async def upsert(self, project_id: str, payload: dict) -> dict:
        """Create-or-update — used by the connect endpoint. Hits the
        UNIQUE(project_id) constraint on insert, so we manually update
        if a row already exists rather than relying on Supabase's
        upsert which is awkward with the surrogate UUID PK.
        """
        return await asyncio.to_thread(self._upsert_sync, project_id, payload)

    def _upsert_sync(self, project_id: str, payload: dict) -> dict:
        existing = self._get_by_project_sync(project_id)
        if existing:
            update = {k: v for k, v in payload.items() if v is not None}
            if not update:
                return existing
            resp = (
                self._sb.table(self.TABLE)
                .update(update)
                .eq("project_id", project_id)
                .execute()
            )
            rows = resp.data or [existing]
            return rows[0]
        insert = {**payload, "project_id": project_id}
        resp = self._sb.table(self.TABLE).insert(insert).execute()
        rows = resp.data or []
        if not rows:
            raise RuntimeError("github_sync_binding insert returned no row")
        return rows[0]

    async def update_watermark(
        self, binding_id: str, *,
        last_pulled_sha: Optional[str] = None,
        last_pulled_at: Optional[str] = None,
        last_pushed_sha: Optional[str] = None,
        last_pushed_at: Optional[str] = None,
    ) -> None:
        update = {
            k: v for k, v in dict(
                last_pulled_sha=last_pulled_sha,
                last_pulled_at=last_pulled_at,
                last_pushed_sha=last_pushed_sha,
                last_pushed_at=last_pushed_at,
            ).items() if v is not None
        }
        if not update:
            return
        await asyncio.to_thread(
            self._update_sync, binding_id, update,
        )

    def _update_sync(self, binding_id: str, update: dict) -> None:
        self._sb.table(self.TABLE).update(update).eq("id", binding_id).execute()

    async def delete_by_project(self, project_id: str) -> bool:
        return await asyncio.to_thread(self._delete_by_project_sync, project_id)

    def _delete_by_project_sync(self, project_id: str) -> bool:
        resp = (
            self._sb.table(self.TABLE)
            .delete()
            .eq("project_id", project_id)
            .execute()
        )
        return bool(resp.data)


class GithubSyncLogRepository:
    """Append-only writer + paginated reader for ``public.synchronize_github_logs``."""

    TABLE = "synchronize_github_logs"

    def __init__(self, client: Optional[SupabaseClient] = None):
        self._sb = (client or SupabaseClient()).client

    async def record(
        self, binding_id: str, *,
        direction: str, status: str,
        git_sha: Optional[str] = None,
        version_commit_id: Optional[str] = None,
        error_message: Optional[str] = None,
        files_changed: Optional[int] = None,
    ) -> dict:
        return await asyncio.to_thread(
            self._record_sync, binding_id, direction, status,
            git_sha, version_commit_id, error_message, files_changed,
        )

    def _record_sync(
        self, binding_id: str, direction: str, status: str,
        git_sha, version_commit_id, error_message, files_changed,
    ) -> dict:
        row = {
            "synchronize_github_binding_id": binding_id,
            "direction": direction,
            "status": status,
            "git_sha": git_sha,
            # Canonical physical column is isolated behind this repository
            # constant; rollout compatibility is handled by the DB trigger.
            GITHUB_SYNC_VERSION_COLUMN: version_commit_id,
            "error_message": error_message,
            "files_changed": files_changed,
        }
        resp = self._sb.table(self.TABLE).insert(row).execute()
        rows = resp.data or []
        if not rows:
            raise RuntimeError("synchronize_github_logs insert returned no row")
        return _to_api_row(rows[0])

    async def list_recent(
        self, binding_id: str, *,
        limit: int = 50, offset: int = 0,
    ) -> tuple[list[dict], int]:
        return await asyncio.to_thread(
            self._list_recent_sync, binding_id, limit, offset,
        )

    def _list_recent_sync(
        self, binding_id: str, limit: int, offset: int,
    ) -> tuple[list[dict], int]:
        resp = (
            self._sb.table(self.TABLE)
            .select("*", count="exact")
            .eq("synchronize_github_binding_id", binding_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return [_to_api_row(row) for row in (resp.data or [])], int(resp.count or 0)

    async def has_successful_sha(
        self, binding_id: str, direction: str, git_sha: str,
    ) -> bool:
        """Webhook idempotency check — has this (integration, direction,
        git_sha) already been imported successfully?"""
        return await asyncio.to_thread(
            self._has_successful_sha_sync, binding_id, direction, git_sha,
        )

    def _has_successful_sha_sync(
        self, binding_id: str, direction: str, git_sha: str,
    ) -> bool:
        return self._find_successful_sha_sync(binding_id, direction, git_sha) is not None

    async def find_successful_sha(self, binding_id: str, direction: str, git_sha: str) -> Optional[dict]:
        return await asyncio.to_thread(self._find_successful_sha_sync, binding_id, direction, git_sha)

    def _find_successful_sha_sync(self, binding_id: str, direction: str, git_sha: str) -> Optional[dict]:
        resp = (
            self._sb.table(self.TABLE)
            .select("*")
            .eq("synchronize_github_binding_id", binding_id)
            .eq("direction", direction)
            .eq("git_sha", git_sha)
            .eq("status", "success")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return dict(rows[0]) if rows else None

    async def latest_successful_import(self, binding_id: str) -> Optional[dict]:
        """Most recent successful import row (carries version_commit_id +
        created_at) — the anchor for GitHub-import conflict detection."""
        return await asyncio.to_thread(self._latest_successful_import_sync, binding_id)

    def _latest_successful_import_sync(self, binding_id: str) -> Optional[dict]:
        resp = (
            self._sb.table(self.TABLE)
            .select("*")
            .eq("synchronize_github_binding_id", binding_id)
            .eq("direction", "inbound")
            .eq("status", "success")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return _to_api_row(rows[0]) if rows else None


def _to_api_row(row: dict) -> dict:
    """The log's persisted parent reference is already canonical."""
    return dict(row)
