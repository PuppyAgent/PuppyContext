"""Bounded app-scoped cache and single-flight for History graph snapshots."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from src.version_engine.read.history_models import HistoryGraphCacheStats, HistoryGraphSnapshot

CacheKey = tuple[str, str]


@dataclass
class _CacheEntry:
    snapshot: HistoryGraphSnapshot
    expires_at: float


@dataclass
class _Flight:
    event: threading.Event
    result: HistoryGraphSnapshot | None = None
    error: BaseException | None = None


class HistoryGraphCache:
    """LRU/TTL cache bounded by retained graph-container weight.

    A snapshot stores three O(N) containers plus graph edges, so the budget
    counts all of them rather than merely limiting the number of repositories.
    In-flight builds are shared without holding the global lock during
    object-store traversal.
    """

    def __init__(
        self,
        *,
        max_snapshots: int = 8,
        max_total_weight: int = 600_000,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_snapshots <= 0 or max_total_weight <= 0 or ttl_seconds <= 0:
            raise ValueError("History graph cache limits must be positive")
        self._max_snapshots = max_snapshots
        self._max_total_weight = max_total_weight
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()
        self._inflight: dict[CacheKey, _Flight] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._waits = 0
        self._builds = 0
        self._evictions = 0
        self._total_weight = 0

    def get_or_build(
        self,
        key: CacheKey,
        builder: Callable[[], HistoryGraphSnapshot],
    ) -> HistoryGraphSnapshot:
        with self._lock:
            self._purge_expired_locked()
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return cached.snapshot
            flight = self._inflight.get(key)
            if flight is None:
                flight = _Flight(event=threading.Event())
                self._inflight[key] = flight
                self._misses += 1
                is_builder = True
            else:
                self._waits += 1
                is_builder = False

        if not is_builder:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.result is None:  # defensive: builder always sets one branch
                raise RuntimeError("history graph single-flight completed without a result")
            return flight.result

        try:
            result = builder()
        except BaseException as exc:
            with self._lock:
                flight.error = exc
                self._inflight.pop(key, None)
                flight.event.set()
            raise

        with self._lock:
            self._builds += 1
            flight.result = result
            self._store_locked(key, result)
            self._inflight.pop(key, None)
            flight.event.set()
        return result

    def stats(self) -> HistoryGraphCacheStats:
        with self._lock:
            self._purge_expired_locked()
            return HistoryGraphCacheStats(
                hits=self._hits,
                misses=self._misses,
                waits=self._waits,
                builds=self._builds,
                evictions=self._evictions,
                entries=len(self._entries),
                total_weight=self._total_weight,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_weight = 0

    def _store_locked(self, key: CacheKey, snapshot: HistoryGraphSnapshot) -> None:
        weight = snapshot.cache_weight
        if weight > self._max_total_weight:
            return
        old = self._entries.pop(key, None)
        if old is not None:
            self._total_weight -= old.snapshot.cache_weight
        self._entries[key] = _CacheEntry(
            snapshot=snapshot,
            expires_at=self._clock() + self._ttl_seconds,
        )
        self._total_weight += weight
        while (
            len(self._entries) > self._max_snapshots or self._total_weight > self._max_total_weight
        ):
            _evicted_key, evicted = self._entries.popitem(last=False)
            self._total_weight -= evicted.snapshot.cache_weight
            self._evictions += 1

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            self._total_weight -= entry.snapshot.cache_weight
            self._evictions += 1
