"""API schemas for durable Integration connections."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class SyncResponse(BaseModel):
    id: str
    project_id: str
    path: Optional[str] = None
    direction: str
    provider: str
    config: dict
    status: str
    last_sync_commit_id: str = ""
    error_message: Optional[str] = None


class SyncStatusItem(BaseModel):
    id: str
    path: Optional[str] = None
    node_name: Optional[str] = None
    node_type: Optional[str] = None
    provider: str
    direction: str
    status: str
    name: Optional[str] = None
    access_key: Optional[str] = None
    trigger: Optional[dict] = None
    last_synced_at: Optional[str] = None
    error_message: Optional[str] = None


class UploadStatusItem(BaseModel):
    id: str
    path: Optional[str] = None
    type: str
    task_type: Optional[str] = None
    status: str
    progress: int = 0
    message: Optional[str] = None
    created_at: Optional[str] = None


class ProjectSyncStatusResponse(BaseModel):
    syncs: list[SyncStatusItem]
    uploads: list[UploadStatusItem]


class BootstrapResponse(BaseModel):
    syncs_created: int


class CreateSyncResponse(BaseModel):
    sync: SyncResponse
    execution_result: Optional[dict] = None


class PullResponse(BaseModel):
    synced: int
    results: list[dict]


class PushResponse(BaseModel):
    pushed: int
    results: list[dict]


class SyncRunResponse(BaseModel):
    id: str
    access_point_id: str
    status: str
    worker_job_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    error: Optional[str] = None
    trigger_type: Optional[str] = None
    result_summary: Optional[str] = None


class FailedSyncRunItem(BaseModel):
    id: str
    access_point_id: str
    access_point_name: Optional[str] = None
    access_point_path: Optional[str] = None
    provider: str = ""
    direction: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    result_summary: Optional[str] = None
    trigger_type: Optional[str] = None


# Credential-bearing keys that the repository stashes inside ``config`` for
# sync execution. They must never be echoed back to API clients.
_REDACTED_CONFIG_KEYS = ("credentials_ref", "access_key")


def _redact_config(config) -> dict | None:
    if not isinstance(config, dict):
        return config
    return {k: v for k, v in config.items() if k not in _REDACTED_CONFIG_KEYS}


def connection_to_response(connection) -> dict:
    return {
        "id": connection.id,
        "project_id": connection.project_id,
        "path": connection.path,
        "direction": connection.direction,
        "provider": connection.provider,
        "config": _redact_config(connection.config),
        "status": connection.status,
        "last_sync_commit_id": connection.last_sync_commit_id,
        "error_message": connection.error_message,
    }
