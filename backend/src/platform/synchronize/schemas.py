"""Operation data for durable Synchronize bindings and runs; no Access identities."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SyncResponse(BaseModel):
    id: str
    project_id: str
    path: Optional[str] = None
    direction: str
    provider: str
    config: dict
    status: str
    last_synchronize_commit_id: str = ""
    error_message: Optional[str] = None
    # Additive management metadata for clients reading real bindings, not Access.
    trigger: dict = Field(default_factory=dict)
    last_synced_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SyncStatusItem(BaseModel):
    id: str
    path: Optional[str] = None
    node_name: Optional[str] = None
    node_type: Optional[str] = None
    provider: str
    direction: str
    status: str
    name: Optional[str] = None
    trigger: Optional[dict] = None
    last_synced_at: Optional[str] = None
    error_message: Optional[str] = None


class ProjectSyncStatusResponse(BaseModel):
    bindings: list[SyncStatusItem]


class BootstrapResponse(BaseModel):
    bindings_created: int


class CreateSyncResponse(BaseModel):
    binding: SyncResponse
    execution_result: Optional[dict] = None


class PullResponse(BaseModel):
    synced: int
    results: list[dict]


class PushResponse(BaseModel):
    pushed: int
    results: list[dict]


class SyncRunResponse(BaseModel):
    id: str
    synchronize_binding_id: str
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
    synchronize_binding_id: str
    synchronize_binding_name: Optional[str] = None
    target_path: Optional[str] = None
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
    config = _redact_config(connection.config)
    trigger = getattr(connection, "trigger", None) or {}
    if getattr(connection, "legacy_read_only_reason", None):
        # Historical configuration is opaque, not an executable/public config
        # contract. Whitelist display metadata rather than guessing secret keys.
        source = config.get("source") if isinstance(config, dict) else None
        name = source.get("resource_name") if isinstance(source, dict) else None
        config = {"source": {"resource_name": name}} if isinstance(name, str) else {}
        trigger = {"type": trigger.get("type", "manual")}
    return {
        "id": connection.id,
        "project_id": connection.project_id,
        "path": connection.path,
        "direction": connection.direction,
        "provider": connection.provider,
        "config": config,
        "status": connection.status,
        "last_synchronize_commit_id": connection.last_synchronize_commit_id,
        "error_message": connection.error_message,
        "trigger": trigger,
        "last_synced_at": getattr(connection, "last_synced_at", None),
        "created_at": getattr(connection, "created_at", None),
        "updated_at": getattr(connection, "updated_at", None),
    }
