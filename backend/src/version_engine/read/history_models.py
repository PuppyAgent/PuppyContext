"""Typed read-model values for all-ref project History."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


class HistoryCursorError(ValueError):
    """The client supplied an invalid or incompatible History cursor."""


class HistorySnapshotUnavailableError(RuntimeError):
    """A valid cursor names a snapshot that can no longer be reconstructed."""


class HistoryRefsUnavailableError(RuntimeError):
    """The control plane could not provide one atomic ref snapshot."""


class HistoryGraphTooLargeError(RuntimeError):
    """A graph exceeded the defensive traversal budget."""


@dataclass(frozen=True)
class HistoryRef:
    ref_name: str
    ref_type: Literal["branch", "tag"]
    commit_id: str


@dataclass(frozen=True)
class GraphCommit:
    commit_id: str
    parent_ids: tuple[str, ...]
    tree_id: str
    author: str
    message: str
    created_at: str | None
    timestamp: int


@dataclass(frozen=True)
class HistoryGraphSnapshot:
    """Immutable topology derived from one exact ordered ref set."""

    order: tuple[str, ...]
    positions: Mapping[str, int]
    nodes: Mapping[str, GraphCommit]
    unreadable_commit_ids: tuple[str, ...]

    @property
    def cache_weight(self) -> int:
        # Retained topology includes three node containers plus parent edges.
        # Counting edges prevents octopus-merge-heavy graphs from bypassing a
        # cache budget expressed only in commit count.
        return (
            len(self.order)
            + len(self.positions)
            + len(self.nodes)
            + sum(len(node.parent_ids) for node in self.nodes.values())
            + len(self.unreadable_commit_ids)
        )


@dataclass(frozen=True)
class HistoryCursorState:
    project_id: str
    snapshot_id: str
    roots: tuple[str, ...]
    head_commit_id: str
    anchor_commit_id: str


@dataclass(frozen=True)
class ProjectHistoryGraphPage:
    entries: list[dict]
    refs: tuple[HistoryRef, ...]
    refs_included: bool
    head_commit_id: str
    snapshot_id: str
    total: int
    next_cursor: str | None
    has_more: bool
    graph_health: Literal["complete", "degraded"]
    unreadable_commit_ids: tuple[str, ...]
    root_hash: str = ""


@dataclass(frozen=True)
class HistoryGraphCacheStats:
    hits: int
    misses: int
    waits: int
    builds: int
    evictions: int
    entries: int
    total_weight: int
