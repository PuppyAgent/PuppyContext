"""
L3-Folder: WorkspaceProvider — Abstract Interface

Defines the unified interface for Agent workspace management.
Concrete implementations vary by platform (macOS APFS / Linux OverlayFS / Fallback full copy).
Conflict resolution logic resides in L2 CollaborationService and is platform-independent.
"""

import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.platform.workspace.sync_models import SyncResult  # L2.5
from src.platform.workspace.paths import validate_storage_segment


@dataclass
class WorkspaceInfo:
    """Workspace information"""
    path: str
    agent_id: str
    project_id: str
    base_commit_id: str | None = None
    lower_path: str = ""


@dataclass
class WorkspaceChanges:
    """Agent's changes"""
    agent_id: str
    base_commit_id: str | None = None
    modified: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)


class WorkspaceProvider(ABC):
    """
    Abstract interface for Agent workspace management

    Each implementation is responsible for:
    1. create_workspace: Create an isolated workspace directory for the Agent
    2. detect_changes: Detect which files the Agent modified
    3. cleanup: Clean up the workspace
    4. sync_lower: Sync S3+PG data to the shared Lower directory
    """

    @abstractmethod
    async def create_workspace(
        self, agent_id: str, project_id: str, base_commit_id: str | None = None
    ) -> WorkspaceInfo:
        """
        Create an isolated workspace for the Agent

        Returns:
            WorkspaceInfo(
                path="/tmp/contextbase/workspaces/{project_id}/{agent_id}",
                ...,
            )
        """
        ...

    @abstractmethod
    async def detect_changes(self, agent_id: str) -> WorkspaceChanges:
        """
        Detect what the Agent changed

        Compare Agent workspace and Lower directory to find modified/new/deleted files.

        Returns:
            WorkspaceChanges(modified={"node_1.json": "{...}"}, deleted=["old.json"])
        """
        ...

    @abstractmethod
    async def cleanup(self, agent_id: str) -> None:
        """Clean up the Agent's workspace"""
        ...

    @abstractmethod
    async def sync_lower(self, project_id: str) -> SyncResult:
        """
        Sync S3+PG data to the local Lower directory

        Incremental sync: compares updated_at, only pulls changed files.
        """
        ...

    @abstractmethod
    def get_lower_path(self, project_id: str) -> str:
        """Get the Lower directory path for the project"""
        ...

    def get_workspace_info(self, agent_id: str) -> WorkspaceInfo | None:
        """Return live process state for one validated Agent workspace."""

        validate_storage_segment(agent_id, label="agent_id")
        registry: dict[str, WorkspaceInfo] = getattr(self, "_registry", {})
        return registry.get(agent_id)

    def snapshot_project_workspaces(self, project_id: str) -> tuple[WorkspaceInfo, ...]:
        """Snapshot only live workspaces owned by the exact Project."""

        validate_storage_segment(project_id, label="project_id")
        registry: dict[str, WorkspaceInfo] = getattr(self, "_registry", {})
        return tuple(info for info in registry.values() if info.project_id == project_id)

    def forget_project_workspaces(self, project_id: str) -> int:
        """Remove an exact Project's stale in-process workspace state."""

        validate_storage_segment(project_id, label="project_id")
        registry: dict[str, WorkspaceInfo] = getattr(self, "_registry", {})
        agent_ids = [
            agent_id
            for agent_id, info in registry.items()
            if info.project_id == project_id
        ]
        for agent_id in agent_ids:
            registry.pop(agent_id, None)
        return len(agent_ids)


_workspace_provider: WorkspaceProvider | None = None
_workspace_provider_key: tuple[str, str] | None = None


def _resolve_provider_type(provider_type: str) -> str:
    if provider_type != "auto":
        return provider_type

    system = platform.system()
    if system == "Darwin":
        return "apfs"
    if system == "Linux":
        return "overlayfs"
    return "fallback"


def get_workspace_provider() -> WorkspaceProvider:
    """
    Automatically select WorkspaceProvider based on platform

    - macOS (Darwin): APFS Clone
    - Linux: Fallback (OverlayFS implementation reserved)
    - Windows / other: Fallback (full copy)
    """
    from src.config import settings

    global _workspace_provider, _workspace_provider_key

    provider_type = _resolve_provider_type(settings.WORKSPACE_PROVIDER)
    base_dir = settings.WORKSPACE_BASE_DIR
    key = (provider_type, base_dir)

    if _workspace_provider is not None and _workspace_provider_key == key:
        return _workspace_provider

    if provider_type == "apfs":
        from src.platform.workspace.apfs_provider import APFSWorkspaceProvider
        _workspace_provider = APFSWorkspaceProvider(base_dir=base_dir)
    elif provider_type == "overlayfs":
        from src.platform.workspace.overlayfs_provider import OverlayFSWorkspaceProvider
        _workspace_provider = OverlayFSWorkspaceProvider(base_dir=base_dir)
    else:
        from src.platform.workspace.fallback_provider import FallbackWorkspaceProvider
        _workspace_provider = FallbackWorkspaceProvider(base_dir=base_dir)

    _workspace_provider_key = key
    return _workspace_provider


def get_active_workspace_provider() -> WorkspaceProvider | None:
    """Return the already-created provider without creating host directories."""

    return _workspace_provider
