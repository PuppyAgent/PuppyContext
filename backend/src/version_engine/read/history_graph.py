"""All-ref project History application service.

This module owns the read-model boundary: one atomic ref snapshot, immutable
Git ancestry, deterministic topology, signed cursor paging, and bounded cache
reuse.  HTTP adapters only validate transport parameters and map typed values
to response schemas.
"""

from __future__ import annotations

import asyncio

from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.history_cache import HistoryGraphCache
from src.version_engine.read.history_cursor import HistoryCursorCodec
from src.version_engine.read.history_models import (
    HistoryCursorState,
    HistoryGraphCacheStats,
    HistorySnapshotUnavailableError,
    ProjectHistoryGraphPage,
)

_MAX_HISTORY_REFS = 512
_DEFAULT_MAX_TRAVERSAL_NODES = 200_000
_MAX_UNREADABLE_IDS_IN_RESPONSE = 20


class HistoryGraphService:
    """App-scoped service for stable, all-branch History pages."""

    def __init__(
        self,
        repo_manager: VersionRepoManager,
        *,
        cache: HistoryGraphCache | None = None,
        cursor_codec: HistoryCursorCodec | None = None,
        max_traversal_nodes: int = _DEFAULT_MAX_TRAVERSAL_NODES,
    ) -> None:
        if max_traversal_nodes <= 0:
            raise ValueError("History graph traversal limit must be positive")
        self._repos = repo_manager
        self._cache = cache or HistoryGraphCache()
        self._cursor_codec = cursor_codec or HistoryCursorCodec()
        self._max_traversal_nodes = max_traversal_nodes

    async def get_page(self, project_id, *, limit, cursor="", grant=None):
        return await asyncio.to_thread(self._native_page, project_id, grant, limit, cursor)

    def _native_page(self, project_id, grant, limit, cursor):
        from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
        from src.version_engine.read.native_history import NativeHistory

        with ProductOperationAdapter(self._repos).open_read(project_id, grant) as reader:
            history = NativeHistory(reader.snapshot, max_commits=self._max_traversal_nodes)
            state = self._cursor_codec.decode(cursor, project_id=project_id) if cursor else None
            start = 0
            if state:
                if state.snapshot_id != history.snapshot_id or state.roots != history.roots:
                    raise HistorySnapshotUnavailableError(
                        "repository refs changed; refresh history"
                    )
                if state.anchor_commit_id not in history.nodes:
                    raise HistorySnapshotUnavailableError("history cursor anchor unavailable")
                start = history.order.index(state.anchor_commit_id) + 1
            page_ids = history.order[start : start + limit]
            more = start + len(page_ids) < len(history.order)
            next_cursor = (
                self._cursor_codec.encode(
                    HistoryCursorState(
                        project_id, history.snapshot_id, history.roots, history.head, page_ids[-1]
                    )
                )
                if more and page_ids
                else None
            )
            return ProjectHistoryGraphPage(
                [history.entry(oid) for oid in page_ids],
                tuple(history.refs) if not cursor else (),
                not bool(cursor),
                history.head,
                history.snapshot_id,
                len(history.order),
                next_cursor,
                more,
                "complete",
                (),
                history.snapshot.revision().tree_oid,
            )

    def cache_stats(self) -> HistoryGraphCacheStats:
        return self._cache.stats()
