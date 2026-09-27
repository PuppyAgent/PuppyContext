from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from src.infra.search.index_task import SearchIndexTask, SearchIndexTaskUpsert
from src.infra.supabase.exceptions import handle_supabase_error

_TABLE = "search_index_tasks"

_STATUS_TO_STORAGE = {
    "pending": "pending",
    "indexing": "running",
    "ready": "completed",
    "error": "failed",
}

_STATUS_FROM_STORAGE = {
    "pending": "pending",
    "running": "indexing",
    "completed": "ready",
    "failed": "error",
    "cancelled": "error",
}


def _row_to_task(row: dict[str, Any]) -> SearchIndexTask:
    """Convert a persisted task row back to the SearchIndexTask domain model."""
    config = row.get("config") or {}
    result = row.get("result") or {}
    return SearchIndexTask(
        id=0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        tool_id=row["id"],
        user_id=row.get("created_by"),
        project_id=row.get("project_id"),
        path=row.get("path") or "",
        json_path=config.get("json_path", ""),
        status=_STATUS_FROM_STORAGE.get(row.get("status", ""), "pending"),
        started_at=row.get("started_at"),
        finished_at=row.get("completed_at"),
        nodes_count=result.get("nodes_count"),
        chunks_count=result.get("chunks_count"),
        indexed_chunks_count=result.get("indexed_chunks_count"),
        last_error=row.get("error"),
        folder_path=config.get("folder_path"),
        total_files=result.get("total_files"),
        indexed_files=result.get("indexed_files"),
    )


class SearchIndexTaskRepository:
    """
    Repository for the dedicated ``search_index_tasks`` table.

    Uses tool_id as the persistent task ID for stable upserts.
    Search-specific fields live in config/result JSONB columns.
    """

    def __init__(self, client: Any):
        self._client = client

    def get_by_tool_id(self, tool_id: str) -> Optional[SearchIndexTask]:
        resp = (
            self._client.table(_TABLE)
            .select("*")
            .eq("id", tool_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        return _row_to_task(rows[0])

    def upsert(self, task: SearchIndexTaskUpsert) -> SearchIndexTask:
        try:
            config: dict[str, Any] = {"tool_id": task.tool_id}
            if task.json_path:
                config["json_path"] = task.json_path
            if task.folder_path:
                config["folder_path"] = task.folder_path

            result: dict[str, Any] = {}
            for key in (
                "nodes_count", "chunks_count", "indexed_chunks_count",
                "total_files", "indexed_files",
            ):
                val = getattr(task, key, None)
                if val is not None:
                    result[key] = val

            status = _STATUS_TO_STORAGE.get(task.status, "pending")

            progress = 0
            if status == "completed":
                progress = 100
            elif task.total_files and task.indexed_files is not None:
                progress = min(100, int(task.indexed_files / task.total_files * 100))
            elif task.chunks_count and task.indexed_chunks_count is not None:
                progress = min(100, int(task.indexed_chunks_count / task.chunks_count * 100))

            payload: dict[str, Any] = {
                "id": task.tool_id,
                "path": task.path,
                "config": config,
                "status": status,
                "progress": progress,
                "updated_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            }
            if task.user_id:
                payload["created_by"] = task.user_id
            if task.project_id:
                payload["project_id"] = task.project_id
            if result:
                payload["result"] = result
            if task.last_error is not None:
                payload["error"] = task.last_error
            if task.started_at is not None:
                payload["started_at"] = (
                    task.started_at.isoformat()
                    if isinstance(task.started_at, dt.datetime)
                    else task.started_at
                )
            if task.finished_at is not None:
                payload["completed_at"] = (
                    task.finished_at.isoformat()
                    if isinstance(task.finished_at, dt.datetime)
                    else task.finished_at
                )

            resp = (
                self._client.table(_TABLE)
                .upsert(payload, on_conflict="id")
                .execute()
            )
            rows = resp.data or []
            if not rows:
                got = self.get_by_tool_id(task.tool_id)
                if got is None:
                    raise ValueError("search_index_tasks upsert returned empty result")
                return got
            return _row_to_task(rows[0])
        except Exception as e:
            raise handle_supabase_error(e, "write to search_index_tasks")
