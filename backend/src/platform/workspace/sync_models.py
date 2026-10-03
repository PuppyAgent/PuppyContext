"""Workspace technical file synchronization, independent of external bindings."""
from dataclasses import dataclass
from pydantic import BaseModel


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
