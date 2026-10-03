"""Neutral source operation inputs/results; no entrypoint lifecycle or scheduler."""
from dataclasses import dataclass, field
from typing import Any

from src.provider._base import Credentials


@dataclass(frozen=True)
class SourceInput:
    """Resolved inputs for one source operation, never a mutable binding row."""
    config: dict[str, Any] = field(default_factory=dict)
    credentials: Credentials = field(default_factory=Credentials)


@dataclass(frozen=True)
class MaterializationInput:
    """Source description plus caller-owned, non-secret output provenance.

    The caller projects and copies only output metadata. Materializers cannot
    update a binding, read credential references or decide scheduling policy.
    """
    source: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class PullResult:
    content: Any
    node_type: str
    remote_hash: str
    summary: str | None = None


@dataclass
class PushResult:
    success: bool
    remote_hash: str | None = None
    error: str | None = None


@dataclass
class ResourceInfo:
    external_resource_id: str
    name: str
    node_type: str
    size_bytes: int | None = None
