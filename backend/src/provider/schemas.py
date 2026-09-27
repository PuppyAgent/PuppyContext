"""
Unified Sync — Data Models

Sync is the in-process DTO for one external provider binding. Persistence is
canonicalized as connections against a Project repository target.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from pydantic import BaseModel


# ============================================================
# Core Model
# ============================================================

@dataclass
class Sync:
    """
    Unified sync binding derived from Project connections.

    Each row represents one sync relationship between a version path
    and an external resource, carrying both connection config and
    sync state.
    """
    id: str
    project_id: str
    path: Optional[str] = None
    direction: str = "inbound"              # inbound | outbound | bidirectional
    provider: str = ""                      # github | notion | gmail | ...
    authority: str = "authoritative"        # authoritative | mirror
    config: Dict[str, Any] = field(default_factory=dict)
    credentials_ref: Optional[str] = None
    access_key: Optional[str] = None
    trigger: Dict[str, Any] = field(default_factory=dict)
    conflict_strategy: Optional[str] = None
    status: str = "active"                  # active | paused | error | syncing
    cursor: Optional[int] = None
    last_synced_at: Optional[str] = None
    error_message: Optional[str] = None
    remote_hash: Optional[str] = None
    last_sync_commit_id: str = ""
    created_by: Optional[str] = None  # Who created the sync (audit field)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ============================================================
# Adapter Return Types
# ============================================================

@dataclass
class PullResult:
    """Returned by adapter.pull() — content pulled from external source."""
    content: Any                    # dict (JSON) or str (markdown)
    node_type: str                  # json | markdown
    remote_hash: str                # SHA-256 of external content
    summary: Optional[str] = None


@dataclass
class PushResult:
    """Returned by adapter.push()."""
    success: bool
    remote_hash: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ResourceInfo:
    """Returned by adapter.list_resources() — one resource in the external source."""
    external_resource_id: str       # resource identifier (relative path / page_id)
    name: str                       # human-readable name
    node_type: str                  # json | markdown | file
    size_bytes: Optional[int] = None


# ============================================================
# SyncWorker (L1 local file incremental sync)
# ============================================================

@dataclass
class SyncResult:
    synced: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class NodeSyncMeta:
    updated_at: str = ""
    name: str = ""
    node_type: str = ""
    file_path: str = ""
    commit_id: str = ""


class SyncProjectRequest(BaseModel):
    project_id: str
    force: bool = False


class SyncProjectResponse(BaseModel):
    project_id: str
    synced: int
    skipped: int
    failed: int
    total: int
    elapsed_seconds: float
