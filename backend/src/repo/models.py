"""Domain models for the repo redesign — pure dataclasses, no DB knowledge.

Repository implementations own
the row ↔ model translation. Routers/services consume these models without
caring whether they came from Supabase, an in-memory store, or a test stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# ──────────────────────────────────────────────────────────────────────────
# Scope
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class RepositoryScope:
    """A real, non-empty path boundary within a Project repository."""

    id: str
    project_id: str
    name: str
    path: str                       # canonical, non-empty, no leading/trailing /
    exclude: list[str]
    max_mode: str                   # 'r' | 'rw'
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedAccessSurfaceCredential:
    """An authenticated CLI Access Surface before Scope geometry is loaded."""

    credential_id: str
    credential_type: str
    access_surface_id: str
    project_id: str
    scope_id: str
    mode_ceiling: str


@dataclass(frozen=True, slots=True)
class ResolvedScopeCredential:
    """A machine credential and its exact, capability-clamped Scope target."""

    credential_id: str
    credential_type: str
    access_surface_id: str
    scope: RepositoryScope

    @property
    def project_id(self) -> str:
        return self.scope.project_id

    @property
    def scope_id(self) -> str:
        return self.scope.id
