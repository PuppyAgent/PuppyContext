"""Synchronize-owned durable binding model (persistence mapping lives in repository)."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SynchronizeBinding:
    id: str
    project_id: str
    path: str | None = None
    direction: str = "inbound"
    provider: str = ""
    authority: str = "authoritative"
    config: dict[str, Any] = field(default_factory=dict)
    credentials_ref: str | None = None
    access_key: str | None = None
    trigger: dict[str, Any] = field(default_factory=dict)
    conflict_strategy: str | None = None
    status: str = "active"
    cursor: int | None = None
    last_synced_at: str | None = None
    error_message: str | None = None
    remote_hash: str | None = None
    last_sync_commit_id: str = ""
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
