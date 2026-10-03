"""Access surface domain model; wire and database legacy names stay at boundaries."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from src.platform.repository_target.models import RepositoryTarget, repository_target_scope_id
# Explicit protocol kinds, not the shared external-source Provider catalog.
ACCESS_KINDS = frozenset({"git_remote", "cli", "agent", "mcp", "mcp_endpoint", "sandbox", "sandbox_endpoint"})

@dataclass
class AccessSurface:
    """Project-root or Scope-bound Access surface domain model."""

    id: str
    target: RepositoryTarget
    kind: str                   # Access protocol kind; legacy source rows are inventory only
    name: str
    direction: str                  # 'bidirectional' | 'inbound' | 'outbound'
    config: dict[str, Any]          # provider-specific
    policy: dict[str, Any]          # connector-specific permission policy
    oauth_connection_id: Optional[int]   # FK → oauth_connections.id (BIGINT)
    trigger: dict[str, Any]         # {"type": "manual" | "scheduled" | "on_change", ...}
    status: str                     # 'active' | 'paused' | 'syncing' | 'error'
    last_run_at: Optional[datetime]
    last_run_id: Optional[str]
    error_message: Optional[str]
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime

    @property
    def project_id(self) -> str:
        return self.target.project_id

    @property
    def scope_id(self) -> Optional[str]:
        return repository_target_scope_id(self.target)

    @property
    def is_builtin(self) -> bool:
        # Standard Access surfaces have dedicated lifecycle operations and
        # cannot be deleted or manually run through the connector facade.
        return self.kind in ("git_remote", "cli", "agent")

    @property
    def is_oauth_backed(self) -> bool:
        # Self-auth providers (raw URL, REST API with API key in config) carry
        # NULL oauth_connection_id; OAuth-backed providers carry a non-NULL one.
        return self.oauth_connection_id is not None

    @property
    def is_access_surface(self) -> bool:
        """Whether this row represents an ongoing Access method.

        Legacy datasource rows used connector-shaped DTOs for one-shot imports.
        Those rows are still useful history/debug state, but they are not
        "ways into" a scope and should not be returned by Access endpoints by
        default.
        """
        return self.kind in ACCESS_KINDS and (self.trigger or {}).get("type") != "import_once"
