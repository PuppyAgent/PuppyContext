"""
S3StorageBackend — S3 implementation of the version ObjectStore

Each project's Git loose objects are stored under the historical S3 namespace
``version/{project_id}/objects/``. That prefix is persisted data layout, not a
runtime protocol boundary.

Performance layers:
  1. CachedStorageBackend — process-wide LRU keyed by content hash (immutable = forever cacheable)
  2. Shared thread pool — reused across all S3 calls instead of per-call creation
  3. S3StorageBackend — actual S3 I/O

Sync/Async strategy:
  The ObjectStore interface is synchronous (get/put/exists).
  PuppyOne's S3Service is asynchronous.
  Synchronous methods bridge via a shared thread pool to avoid nested asyncio.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import cachetools

from src.infra.s3.service import S3Service
from src.infra.supabase.client import SupabaseClient
from src.utils.logger import log_error, log_warning
from src.version_engine.domain.errors import ObjectNotFoundError, StorageWriteError
from src.version_engine.infrastructure.supabase import safe_data
from src.version_engine.infrastructure.supabase.db_names import OBJECT_LOCATIONS_TABLE
from src.version_engine.storage.chunk_manifest import (
    chunk_manifest_root,
    chunk_upload_plan,
    validate_chunk_manifest,
)
from src.version_engine.storage.io_strategy import IOStorageStrategy, ObjectWriteLayout
from src.version_engine.storage.mutation_context import collection_context, publication_context
from src.version_engine.storage.object_store import StorageBackend
from src.version_engine.write_engine.git_object_format import decode_object, hash_object
from src.version_engine.write_engine.trace import trace_mark, trace_phase

# Sync callers use this bridge for object flushes and reads. Large Git pushes
# can legitimately fan out into many S3 writes plus location-index upserts; if
# this budget is too small, the client sees a reject while the async task may
# still be finishing in the background. Keep it above per-call S3 read_timeout.
_ASYNC_BRIDGE_TIMEOUT_SECS = 1800
_HASH_PREFIX_LEN = 2
_MAX_LIST_KEYS = 10000
_BUNDLE_MAGIC = b"POB1"
_BUNDLE_HEADER_LEN_BYTES = 8
_CANONICAL_STORAGE_NAMESPACE = "version"
_CHUNKED_PACK_PREFIX = "chunked:"
# Keep each physical S3 object below S3Service's multipart threshold. Supabase
# Storage's S3-compatible multipart path is materially slower and can fail on
# large Git pushes; small immutable bundle/chunk objects are more predictable.
_OBJECT_BUNDLE_TARGET_BYTES = 8 * 1024 * 1024
_OBJECT_CHUNK_BYTES = 8 * 1024 * 1024
_OBJECT_UPLOAD_CONCURRENCY = 8
_OBJECT_LOCATION_UPSERT_BATCH_SIZE = 200

_BRIDGE_LOOP: asyncio.AbstractEventLoop | None = None
_BRIDGE_LOCK = threading.Lock()


def _get_bridge_loop() -> asyncio.AbstractEventLoop:
    """Lazily create a single persistent event loop running on a background thread."""
    global _BRIDGE_LOOP
    if _BRIDGE_LOOP is None or _BRIDGE_LOOP.is_closed():
        with _BRIDGE_LOCK:
            if _BRIDGE_LOOP is None or _BRIDGE_LOOP.is_closed():
                loop = asyncio.new_event_loop()
                t = threading.Thread(
                    target=loop.run_forever,
                    daemon=True,
                    name="version-s3-loop",
                )
                t.start()
                _BRIDGE_LOOP = loop
    return _BRIDGE_LOOP


def _run_async(coro):
    """Execute an async coroutine from a synchronous context via a persistent loop."""
    loop = _get_bridge_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=_ASYNC_BRIDGE_TIMEOUT_SECS)


# ═══════════════════════════════════════════════
# CachedStorageBackend — process-wide LRU cache
# ═══════════════════════════════════════════════

# LRU is keyed by content hash, so cached objects are immutable —
# we can cache aggressively and the only cost is RAM. Tree nodes are
# tiny (a few KB) but blob payloads can be tens of MB on real
# projects, and version submissions can hit them repeatedly while flattening
# incoming/current/base trees for server-side merge and CAS retry.
# A 1 MB threshold meant a 26 MB user blob fell through both passes
# and triggered two 19-second S3 GETs per push. Lifting the
# per-object threshold to ~64 MB lets large blobs land in the LRU
# after the first push, so subsequent writes don't pay the round-
# trip again. Total budget bumped to keep room for a handful of
# large blobs alongside the tree nodes.
_CACHE_MAX_BYTES = 512 * 1024 * 1024  # 512 MB total budget
_CACHEABLE_THRESHOLD = 64 * 1024 * 1024  # cache up to 64 MB per object

_global_cache: cachetools.LRUCache | None = None
_cache_lock = threading.Lock()
_ACTIVE_WRITE_BATCH: ContextVar[ObjectWriteBatch | None] = ContextVar(
    "version_object_write_batch",
    default=None,
)


@dataclass(frozen=True)
class ObjectLocation:
    pack_key: str
    offset_bytes: int
    size_bytes: int


@dataclass(frozen=True)
class ObjectStorageLayout:
    """Physical S3 layout for one project's version objects.

    All runtime reads and writes use the canonical namespace. Historical
    formats are handled exclusively by operator data migration artifacts.
    """

    project_id: str
    primary_namespace: str = _CANONICAL_STORAGE_NAMESPACE

    @property
    def object_prefix(self) -> str:
        return f"{self.primary_namespace}/{self.project_id}/objects"

    @property
    def bundle_prefix(self) -> str:
        return f"{self.primary_namespace}/{self.project_id}/object-bundles"


def _encode_object_bundle(objects: dict[str, bytes]) -> tuple[bytes, list[dict]]:
    """Encode a batch of Git loose objects into one immutable bundle."""

    body = bytearray()
    entries: list[dict] = []
    for object_id, data in sorted(objects.items()):
        offset = len(body)
        body.extend(data)
        entries.append(
            {
                "object_id": object_id,
                "offset_bytes": offset,
                "size_bytes": len(data),
            }
        )
    header = json.dumps(
        {"version": 1, "objects": entries},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    bundle = _BUNDLE_MAGIC + struct.pack(">Q", len(header)) + header + bytes(body)
    data_offset = len(_BUNDLE_MAGIC) + _BUNDLE_HEADER_LEN_BYTES + len(header)
    for entry in entries:
        entry["offset_bytes"] += data_offset
    return bundle, entries


def _get_global_cache() -> cachetools.LRUCache:
    global _global_cache
    if _global_cache is None:
        with _cache_lock:
            if _global_cache is None:
                _global_cache = cachetools.LRUCache(
                    maxsize=_CACHE_MAX_BYTES,
                    getsizeof=len,
                )
    return _global_cache


class CachedStorageBackend(StorageBackend):
    """In-memory LRU cache in front of any StorageBackend.

    Content-addressed objects are immutable by definition:
    same hash = same content, forever. No TTL needed.

    The cache object is process-wide, but entries are namespaced by
    physical backend. S3 object paths are per-project, so a blob cached
    after reading project A is not evidence that project B has the same
    object in its own namespace.
    All cache access is protected by _cache_lock because
    cachetools.LRUCache is not thread-safe.
    """

    def __init__(self, inner: StorageBackend):
        self._inner = inner
        self._cache = _get_global_cache()
        self._cache_namespace = (
            getattr(inner, "_cache_namespace", None)
            or getattr(inner, "_prefix", None)
            or getattr(inner, "_project_id", None)
            or id(inner)
        )

    @property
    def publication_project_id(self) -> str | None:
        return self._inner.publication_project_id

    def pinned_reader(self, snapshot):
        return self._inner.pinned_reader(snapshot)

    def durable_readback(self):
        return self._inner.durable_readback()

    def get_durable(self, h: str) -> bytes:
        return self._inner.get_durable(h)

    def put_durable(self, h: str, loose_bytes: bytes) -> None:
        self._inner.put_durable(h, loose_bytes)
        self._remember_cached(h, loose_bytes)

    def put_many_durable(self, objects: dict[str, bytes]) -> None:
        self._inner.put_many_durable(objects)
        for oid, data in objects.items():
            self._remember_cached(oid, data)

    def _cache_key(self, h: str):
        return (self._cache_namespace, h)

    def _remember_cached(self, h: str, data: bytes) -> None:
        if len(data) < _CACHEABLE_THRESHOLD:
            with _cache_lock:
                self._cache[self._cache_key(h)] = data

    def get(self, h: str) -> bytes:
        active_batch = _ACTIVE_WRITE_BATCH.get()
        if active_batch is not None and active_batch.backend is self:
            pending = active_batch.get(h)
            if pending is not None:
                trace_mark("object.cache.hit", object_id=h[:12], cache="write_batch")
                return pending
        with _cache_lock:
            cached = self._cache.get(self._cache_key(h))
        if cached is not None:
            trace_mark(
                "object.cache.hit",
                object_id=h[:12],
                cache="memory",
                size_bytes=len(cached),
            )
            return cached
        with trace_phase("object.cache.miss_remote_get", object_id=h[:12]):
            data = self._inner.get(h)
        self._remember_cached(h, data)
        return data

    def get_range(self, h: str, start: int = 0, limit: int | None = None) -> tuple[bytes, int]:
        """Return a byte range without forcing a full download when possible."""
        with _cache_lock:
            cached = self._cache.get(self._cache_key(h))
        if cached is not None:
            end = len(cached) if limit is None else min(len(cached), start + limit)
            return cached[start:end], len(cached)

        get_range = getattr(self._inner, "get_range", None)
        if callable(get_range):
            return get_range(h, start=start, limit=limit)

        data = self.get(h)
        end = len(data) if limit is None else min(len(data), start + limit)
        return data[start:end], len(data)

    def get_many(self, hashes: list[str]) -> dict[str, bytes]:
        return _run_async(self.async_get_many(hashes))

    def put(self, h: str, data: bytes) -> None:
        # The memory cache is a read-through byte cache, not a durable
        # existence proof. Writes must always reach the physical backend
        # for this project namespace; otherwise cross-project cache hits
        # or failed batch flushes can publish trees whose blobs were never
        # materialized in S3.
        active_batch = _ACTIVE_WRITE_BATCH.get()
        if active_batch is not None and active_batch.backend is self:
            active_batch.put(h, data)
            return
        with trace_phase("object.remote_put", object_id=h[:12], size_bytes=len(data)):
            self._inner.put(h, data)
        self._remember_cached(h, data)

    def exists(self, h: str) -> bool:
        active_batch = _ACTIVE_WRITE_BATCH.get()
        if active_batch is not None and active_batch.backend is self and active_batch.has(h):
            return True
        return self._inner.exists(h)

    def exists_many(self, hashes: list[str]) -> set[str]:
        active_batch = _ACTIVE_WRITE_BATCH.get()
        existing: set[str] = set()
        remaining: list[str] = []
        for h in hashes:
            if active_batch is not None and active_batch.backend is self and active_batch.has(h):
                existing.add(h)
            else:
                remaining.append(h)
        if remaining:
            existing.update(self._inner.exists_many(remaining))
        return existing

    async def async_get_many(self, hashes: list[str], concurrency: int = 20) -> dict[str, bytes]:
        unique = list(dict.fromkeys(hashes))
        results: dict[str, bytes] = {}
        remaining: list[str] = []
        active_batch = _ACTIVE_WRITE_BATCH.get()
        with _cache_lock:
            for h in unique:
                if active_batch is not None and active_batch.backend is self:
                    pending = active_batch.get(h)
                    if pending is not None:
                        results[h] = pending
                        continue
                cached = self._cache.get(self._cache_key(h))
                if cached is not None:
                    results[h] = cached
                else:
                    remaining.append(h)

        getter = getattr(self._inner, "async_get_many", None)
        if callable(getter) and remaining:
            fetched = await getter(remaining, concurrency=concurrency)
        else:
            import asyncio

            sem = asyncio.Semaphore(concurrency)
            fetched: dict[str, bytes] = {}

            async def _fetch(h: str) -> None:
                async with sem:
                    fetched[h] = await asyncio.to_thread(self._inner.get, h)

            await asyncio.gather(*[_fetch(h) for h in remaining])

        with _cache_lock:
            for h, data in fetched.items():
                if len(data) < _CACHEABLE_THRESHOLD:
                    self._cache[self._cache_key(h)] = data
        results.update(fetched)
        return results

    def all_hashes(self) -> list[str]:
        return self._inner.all_hashes()

    def all_hashes_with_metadata(self) -> dict[str, dict]:
        getter = getattr(self._inner, "all_hashes_with_metadata", None)
        if callable(getter):
            return getter()
        return {h: {} for h in self.all_hashes()}

    def count(self) -> tuple[int, int]:
        return self._inner.count()

    def delete(self, h: str) -> bool:
        with _cache_lock:
            self._cache.pop(self._cache_key(h), None)
        return self._inner.delete(h)

    def sweep_dead_bundles(self, dead_object_ids) -> tuple[int, list[str]]:
        sweep = getattr(self._inner, "sweep_dead_bundles", None)
        if not callable(sweep):
            return 0, []
        count, swept = sweep(dead_object_ids)
        if swept:
            with _cache_lock:
                for object_id in swept:
                    self._cache.pop(self._cache_key(object_id), None)
        return count, swept

    @contextmanager
    def stage_object_writes(self):
        batch = ObjectWriteBatch(self)
        token = _ACTIVE_WRITE_BATCH.set(batch)
        try:
            yield batch
        finally:
            _ACTIVE_WRITE_BATCH.reset(token)


class ObjectWriteBatch:
    """Stage content-addressed object writes and flush them as one batch."""

    def __init__(self, backend: CachedStorageBackend):
        self.backend = backend
        self._objects: dict[str, bytes] = {}
        self._flushed_ids: list[str] = []

    def put(self, h: str, data: bytes) -> None:
        _verify_loose_hash(h, data)
        self._objects[h] = data

    def get(self, h: str) -> bytes | None:
        return self._objects.get(h)

    def has(self, h: str) -> bool:
        return h in self._objects

    def count(self) -> int:
        return len(self._objects)

    @property
    def flushed_ids(self) -> list[str]:
        return list(self._flushed_ids)

    def flush(self) -> None:
        objects = dict(self._objects)
        if not objects:
            return
        inner = self.backend._inner
        async_put_many = getattr(inner, "async_put_many", None)
        with trace_phase(
            "object.batch.flush",
            count=len(objects),
            bytes=sum(len(data) for data in objects.values()),
        ):
            if callable(async_put_many):
                _run_async(async_put_many(objects, skip_exists=True))
            else:
                for h, data in objects.items():
                    inner.put(h, data)
        for h, data in objects.items():
            self.backend._remember_cached(h, data)
        self._flushed_ids = list(objects)
        self._objects.clear()


@contextmanager
def stage_object_writes(store_or_backend):
    """Stage ObjectStore writes for a synchronous version transaction.

    A Git object id is a hash of the object body, so accepted operation
    writes can safely compute all blob/tree/commit ids first, then upload
    those immutable objects in parallel immediately before publishing the
    scope ref. This keeps the publish boundary synchronous while removing
    avoidable serial S3 latency from tiny CLI writes.
    """
    backend = getattr(store_or_backend, "_backend", store_or_backend)
    if not isinstance(backend, CachedStorageBackend):
        yield None
        return

    with backend.stage_object_writes() as batch:
        yield batch


def _verify_loose_hash(expected_hash: str, data: bytes) -> tuple[str, int]:
    try:
        obj_type, content = decode_object(data)
        actual_hash = hash_object(
            obj_type, content, object_format="sha256" if len(expected_hash) == 64 else "sha1"
        )
    except Exception as e:
        raise StorageWriteError(f"invalid git loose object for {expected_hash}: {e}") from e
    if actual_hash != expected_hash:
        raise StorageWriteError(
            f"content-addressed object mismatch: expected {expected_hash}, got {actual_hash}",
        )
    return obj_type, len(content)


# ═══════════════════════════════════════════════
# S3StorageBackend — actual S3 I/O
# ═══════════════════════════════════════════════


class S3StorageBackend(StorageBackend):
    """S3 backend for the version ObjectStore, isolated by project_id."""

    def __init__(
        self,
        s3: S3Service,
        project_id: str,
        *,
        supabase: SupabaseClient | None = None,
        storage_layout: ObjectStorageLayout | None = None,
        io_strategy: IOStorageStrategy | None = None,
        require_immutable_chunks: bool = False,
    ):
        self._require_immutable_chunks = require_immutable_chunks
        self._s3 = s3
        self._project_id = project_id
        self._supabase = supabase
        if storage_layout is not None:
            self._layout = storage_layout
        else:
            self._layout = ObjectStorageLayout(project_id)
        self._prefix = self._layout.object_prefix
        self._bundle_prefix = self._layout.bundle_prefix
        self._io_strategy = io_strategy or IOStorageStrategy(
            bundle_target_bytes=_OBJECT_BUNDLE_TARGET_BYTES,
            chunk_bytes=_OBJECT_CHUNK_BYTES,
            location_index_enabled=self._supabase is not None,
        )
        self._location_cache: dict[str, ObjectLocation] = {}
        self._location_lock = threading.Lock()

    @property
    def publication_project_id(self) -> str:
        if self._layout.project_id != self._project_id:
            raise ValueError("object storage Project binding mismatch")
        if self._layout.primary_namespace != _CANONICAL_STORAGE_NAMESPACE:
            raise ValueError("native publication requires the canonical object namespace")
        return self._project_id

    def pinned_reader(self, snapshot):
        from src.version_engine.storage.pinned_reader import PinnedObjectReader

        if snapshot.project_id != self.publication_project_id:
            raise StorageWriteError("pinned reader Project mismatch")
        snapshot.check_live()
        rows = snapshot.control.call(
            "get_version_pinned_object_locations",
            p_project_id=snapshot.project_id,
            p_actor=snapshot.actor,
            p_pin_id=snapshot.pin,
        )
        reader = S3StorageBackend(
            self._s3,
            self._project_id,
            supabase=self._supabase,
            io_strategy=self._io_strategy,
            require_immutable_chunks=True,
        )
        locations = {}
        for row in rows:
            location = ObjectLocation(row["pack_key"], row["offset_bytes"], row["size_bytes"])
            reader._validate_location(location)
            locations[row["object_id"]] = location
        reader._pinned_locations = locations
        return PinnedObjectReader(reader, snapshot)

    def durable_readback(self):
        return _S3DurableReadback(self)

    def get_durable(self, h: str) -> bytes:
        # A location cached before compaction/deletion is not current proof.
        reader = S3StorageBackend(
            self._s3,
            self._project_id,
            supabase=self._supabase,
            io_strategy=self._io_strategy,
            require_immutable_chunks=True,
        )
        location = reader._lookup_object_location(h)
        if location is not None:
            key = location.pack_key.removeprefix(_CHUNKED_PACK_PREFIX)
            if not key.startswith(reader._bundle_prefix + "/"):
                raise StorageWriteError(
                    "publication object location is outside its canonical Project namespace"
                )
        return reader._get_with_location(h, location)

    def _key_for(self, h: str) -> str:
        return f"{self._prefix}/{h[:_HASH_PREFIX_LEN]}/{h[_HASH_PREFIX_LEN:]}"

    def _bundle_key_for(self, bundle_bytes: bytes) -> str:
        digest = hashlib.sha256(bundle_bytes).hexdigest()
        return f"{self._bundle_prefix}/{digest[:_HASH_PREFIX_LEN]}/{digest}.pob"

    def _chunk_bundle_prefixes(self) -> tuple[str, ...]:
        return (self._bundle_prefix,)

    def _check_publication_context(self) -> None:
        context = publication_context.get()
        if context is not None and context.project_id != self.publication_project_id:
            raise StorageWriteError("publication context belongs to another Project")

    def _physical_s3(self) -> S3Service:
        if publication_context.get() is None and collection_context.get() is None:
            return self._s3
        factory = getattr(self._s3, "for_single_attempt_io", None)
        if not callable(factory):
            raise StorageWriteError("native single-attempt storage capability unavailable")
        return factory()

    def _authorize_deletion(self) -> None:
        context = collection_context.get()
        if context is not None and context.project_id != self.publication_project_id:
            raise StorageWriteError("collection context belongs to another Project")
        if self._supabase is None:
            if context is not None:
                raise StorageWriteError("native deletion requires database coordination")
            return  # Existing standalone legacy backend, not native authority.
        try:
            response = self._supabase.client.rpc(
                "authorize_version_object_deletion",
                {
                    "p_project_id": self._project_id,
                    "p_gc_token": context.token if context is not None else None,
                },
            ).execute()
            if response.data is not True:
                raise StorageWriteError("object deletion was not authorized")
        except Exception as exc:
            raise StorageWriteError(f"object deletion coordination failed: {exc}") from exc

    # ── Sync methods called by ObjectStore ──

    def get(self, h: str) -> bytes:
        location = self._lookup_object_location(h)
        return self._get_with_location(h, location)

    def _get_with_location(self, h: str, location: ObjectLocation | None) -> bytes:
        """Reuse this attempt's lookup, including absence; retries may refresh it."""
        if location is not None:
            return self._get_packed_object_at(h, location)
        try:
            with trace_phase("s3.get", object_id=h[:12]):
                data = _run_async(self._s3.download_file(self._key_for(h)))
        except ObjectNotFoundError as exc:
            return self._get_packed_object(h, cause=exc)
        except Exception as e:
            if _is_not_found_error(e):
                return self._get_packed_object(h, cause=e)
            raise
        # Verify primary-namespace bytes; see ``async_get`` for the
        # full rationale (520885e2 read-side fix). Stale bytes fall
        # through to indexed bundle instead of reaching zlib.
        try:
            _verify_loose_hash(h, data)
        except StorageWriteError as verify_err:
            log_warning(
                f"[VersionS3] stale primary-namespace bytes at "
                f"{self._key_for(h)} (hash={h[:12]}): {verify_err}. "
                f"Falling through to indexed bundle lookup.",
            )
            return self._get_packed_object(h, cause=verify_err)
        return data

    def get_range(self, h: str, start: int = 0, limit: int | None = None) -> tuple[bytes, int]:
        location = self._lookup_object_location(h)
        if location is not None:
            data = self._get_packed_object_at(h, location)
            end = len(data) if limit is None else min(len(data), start + limit)
            return data[start:end], len(data)
        # Primary loose objects are small (< the bundle/chunk threshold),
        # so we read the whole object through the verified ``get`` path
        # and slice. A partial ``download_file_range`` could not be
        # hash-verified, which would reopen the 520885e2 hole for range
        # reads; reusing ``get`` keeps the verification + indexed bundle
        # fall-through in one place.
        data = self._get_with_location(h, location)
        end = len(data) if limit is None else min(len(data), start + limit)
        return data[start:end], len(data)

    def get_many(self, hashes: list[str]) -> dict[str, bytes]:
        return _run_async(self.async_get_many(hashes))

    def put(self, h: str, data: bytes) -> None:
        try:
            with trace_phase("s3.put", object_id=h[:12], size_bytes=len(data)):
                _run_async(self.async_put(h, data))
        except Exception as e:
            log_error(f"[VersionS3] Failed to put {h}: {e}")
            raise StorageWriteError(f"failed to write object {h} to S3: {e}") from e

    def put_many_durable(self, objects: dict[str, bytes]) -> None:
        # The ordinary batch implementation reserves/settles capacity and awaits
        # every physical/index write. Do not stage, bypass admission or skip proof.
        if objects:
            _run_async(self.async_put_many(objects, concurrency=8))

    def exists(self, h: str) -> bool:
        if self._lookup_object_location(h) is not None:
            return True
        try:
            if _run_async(self._s3.file_exists(self._key_for(h))):
                return True
            return self._lookup_object_location(h) is not None
        except Exception as e:
            if _is_not_found_error(e):
                return self._lookup_object_location(h) is not None
            raise

    def exists_many(self, hashes: list[str]) -> set[str]:
        unique = list(dict.fromkeys(hashes))
        existing: set[str] = set()
        remaining: list[str] = []
        for h in unique:
            if self._cached_object_location(h) is not None:
                existing.add(h)
            else:
                remaining.append(h)

        if self._supabase is not None and remaining:
            try:
                with trace_phase("db.object_location.lookup_many", count=len(remaining)):
                    existing.update(self._lookup_many_object_locations(remaining).keys())
            except Exception:
                pass

        remaining = [h for h in remaining if h not in existing]
        if remaining:
            # ALWAYS allow the per-hash fallback to re-check packed
            # locations, even when the bulk Supabase lookup already
            # "completed". A successful bulk query that returns N-k
            # rows is ambiguous: the missing k entries could mean
            # "object really doesn't exist" OR "row not yet visible
            # from this connection" (Postgres replica lag, transient
            # connection rotation, or the read happening on a
            # different worker before the cache propagated).
            #
            # If we trust the bulk result as authoritative, a bundled
            # object whose row just missed the cutoff is reported as
            # missing — and ``s3.file_exists(loose_key)`` returns
            # False because the bundle has no per-hash key. That
            # cascade is what flipped the project view to
            # ``current_corrupt`` immediately after a move op on
            # staging, then self-healed on the next commit when the
            # row finally became visible.
            #
            # The cost of always re-checking is one extra
            # ``_lookup_object_location`` call per still-missing hash,
            # i.e. exactly one extra Supabase round-trip per hash that
            # the bulk query already missed. In the happy path this
            # branch is never taken because ``remaining`` is empty.
            existing.update(
                _run_async(
                    self.async_exists_many(
                        remaining,
                        concurrency=20,
                        check_packed_locations=True,
                    )
                )
            )
        return existing

    def all_hashes(self) -> list[str]:
        # GC enumerates every stored object id. Loose objects live under
        # the ``objects/`` prefix; bundled (.pob) and chunked objects have
        # NO per-hash S3 key — their ids live only in the location index.
        # Listing the loose prefix alone (the historical behaviour) hid
        # every batch-written object from GC, so bundled orphans from
        # CAS-race losers accumulated forever (GAP-2).
        try:
            seen: set[str] = set()
            hashes: list[str] = []
            for item in self._list_all_object_items():
                object_id = self._hash_from_key(item.key)
                if object_id and object_id not in seen:
                    seen.add(object_id)
                    hashes.append(object_id)
            for object_id in self._all_packed_locations():
                if object_id not in seen:
                    seen.add(object_id)
                    hashes.append(object_id)
            return hashes
        except Exception as e:
            log_error(f"[VersionS3] Failed to list hashes: {e}")
            raise

    def all_hashes_with_metadata(self) -> dict[str, dict]:
        """Return object ids and metadata (``last_modified``/``size``)
        needed by conservative GC, across loose AND packed objects."""
        try:
            result: dict[str, dict] = {}
            for item in self._list_all_object_items():
                object_id = self._hash_from_key(item.key)
                if object_id:
                    result[object_id] = {
                        "last_modified": item.last_modified,
                        "size": item.size,
                    }
            # Packed objects: ``created_at`` is the age signal the
            # retention window needs; the per-hash S3 listing can't see
            # them because they share a pack file.
            for object_id, meta in self._all_packed_locations().items():
                result.setdefault(
                    object_id,
                    {
                        "last_modified": meta.get("last_modified"),
                        "size": meta.get("size", 0),
                    },
                )
            return result
        except Exception as e:
            log_error(f"[VersionS3] Failed to list hash metadata: {e}")
            raise

    def _all_packed_locations(self) -> dict[str, dict]:
        """Every bundled/chunked object for this project, from the
        location index. Returns ``{object_id: {pack_key, size,
        last_modified}}``.

        Loose objects are absent from this table (they have real S3
        keys). Returns ``{}`` when no location index is configured (the
        in-memory/test backends), preserving the loose-only behaviour.
        """
        if self._supabase is None:
            return {}
        out: dict[str, dict] = {}
        page = 1000
        start = 0
        while True:
            with trace_phase("db.object_location.list_all", start=start):
                resp = (
                    self._supabase.client.table(OBJECT_LOCATIONS_TABLE)
                    .select("object_id, pack_key, size_bytes, created_at")
                    .eq("project_id", self._project_id)
                    .range(start, start + page - 1)
                    .execute()
                )
            rows = safe_data(resp) or []
            for row in rows:
                object_id = str(row.get("object_id") or "")
                if object_id:
                    out[object_id] = {
                        "pack_key": str(row.get("pack_key") or ""),
                        "size": int(row.get("size_bytes") or 0),
                        "last_modified": row.get("created_at"),
                    }
            if len(rows) < page:
                return out
            start += page

    def _list_all_object_items(self) -> list:
        items = []
        token = None
        while True:
            page, _, token, truncated = _run_async(
                self._s3.list_files(
                    prefix=f"{self._prefix}/",
                    max_keys=_MAX_LIST_KEYS,
                    continuation_token=token,
                )
            )
            items.extend(page)
            if not truncated or not token:
                return items

    def _hash_from_key(self, key: str) -> str:
        parts = key.removeprefix(f"{self._prefix}/").split("/")
        if len(parts) != 2:
            return ""
        return parts[0] + parts[1]

    def count(self) -> tuple[int, int]:
        """Return ``(object_count, total_bytes)`` across loose AND packed
        objects.

        The byte total used to be hard-coded to ``0`` (GAP-15) even though
        the object listing already carries per-object sizes. We now sum the
        sizes ``all_hashes_with_metadata`` already collects — loose sizes
        come from the S3 listing, packed sizes from the location index — so
        the total is accurate at no extra cost over the count itself. (S3
        has no O(1) count/size API for the loose prefix, so enumerating the
        keys remains inherent; nothing on a hot path calls this.)
        """
        meta = self.all_hashes_with_metadata()
        total_bytes = sum(int(m.get("size") or 0) for m in meta.values())
        return len(meta), total_bytes

    def delete(self, h: str) -> bool:
        """Delete one object. Routes by physical layout (GAP-2).

        Loose objects delete their S3 key. Chunked objects are
        standalone (own manifest + parts) so they delete cleanly.
        Bundled (.pob) objects share a pack file with other objects and
        CANNOT be removed individually without repacking — those are
        refused here and collected only by ``sweep_dead_bundles`` when
        the whole bundle is dead.
        """
        if len(h) not in {40, 64} or not set(h) <= set("0123456789abcdef"):
            raise StorageWriteError("invalid object id for deletion")
        self._authorize_deletion()
        location = self._lookup_object_location(h)
        if location is None:
            return self._delete_loose(h)
        if location.pack_key.startswith(_CHUNKED_PACK_PREFIX):
            return self._delete_chunked(h, location)
        log_warning(
            f"[VersionS3] refusing per-object delete of bundled object "
            f"{h[:12]} in pack {location.pack_key}; whole-bundle sweep "
            f"handles shared bundles.",
        )
        return False

    def _delete_loose(self, h: str) -> bool:
        self.invalidate_proofs([h])
        deleted = True
        try:
            _run_async(self._physical_s3().delete_file(self._key_for(h)))
        except Exception as e:
            if _is_not_found_error(e):
                deleted = False
            else:
                log_error(f"[VersionS3] Failed to delete {h}: {e}")
                raise
        self._collect_capacity([h])
        return deleted

    def _delete_chunked(self, h: str, location: ObjectLocation) -> bool:
        self.invalidate_proofs([h])
        manifest_key = location.pack_key.removeprefix(_CHUNKED_PACK_PREFIX)
        keys = self._chunked_keys_for(h, manifest_key, location.size_bytes)
        deleted_any = False
        for key in keys:
            try:
                _run_async(self._physical_s3().delete_file(key))
                deleted_any = True
            except Exception as exc:
                if not _is_not_found_error(exc):
                    log_error(f"[VersionS3] delete chunk key {key}: {exc}")
                    raise  # Unknown deletion outcome must retain the GC fence.
        self._delete_object_location_rows([h])
        with self._location_lock:
            self._location_cache.pop(h, None)
        return deleted_any

    def _chunked_keys_for(self, h: str, manifest_key: str, size: int) -> list[str]:
        """Validate the complete deletion set before any DELETE.

        Only an absent manifest permits listing the owned object prefix.
        Corruption, foreign keys and unavailable storage fail closed instead.
        """
        prefixes = (
            (self._bundle_prefix,)
            if collection_context.get() is not None
            else self._chunk_bundle_prefixes()
        )
        root = chunk_manifest_root(manifest_key, h, prefixes)
        try:
            manifest_raw = _run_async(self._s3.download_file(manifest_key))
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            keys = [manifest_key]
            token = None
            while True:
                page, _, token, truncated = _run_async(
                    self._s3.list_files(
                        prefix=root + "/",
                        max_keys=_MAX_LIST_KEYS,
                        continuation_token=token,
                    )
                )
                for item in page:
                    if not item.key.startswith(root + "/"):
                        raise StorageWriteError("chunk listing escaped its object namespace")
                    keys.append(item.key)
                if not truncated:
                    break
                if not token:
                    raise StorageWriteError("incomplete chunk listing")
            return list(dict.fromkeys(keys))
        _immutable, chunks = validate_chunk_manifest(
            manifest_raw,
            key=manifest_key,
            oid=h,
            size=size,
            bundle_prefixes=prefixes,
        )
        return list(dict.fromkeys([manifest_key, *(chunk["key"] for chunk in chunks)]))

    def sweep_dead_bundles(self, dead_object_ids) -> tuple[int, list[str]]:
        """Delete whole ``.pob`` bundles all of whose members are dead.

        ``dead_object_ids`` is the GC's eligible-for-deletion set. A
        bundle packs many objects; we can only drop it when EVERY object
        it contains is eligible — otherwise a live object would be lost.
        Partially-dead bundles are kept (and logged); reclaiming their
        dead members would require repacking, which is out of scope.

        Returns ``(member_count_swept, swept_object_ids)``.
        """
        dead = set(dead_object_ids)
        if self._supabase is None or not dead:
            return 0, []
        locations = self._lookup_many_object_locations(list(dead))
        bundle_keys = {
            loc.pack_key
            for loc in locations.values()
            if loc.pack_key and not loc.pack_key.startswith(_CHUNKED_PACK_PREFIX)
        }
        swept: list[str] = []
        kept_partial = 0
        for pack_key in bundle_keys:
            members = self._object_ids_in_pack(pack_key)
            if not members:
                continue
            if not all(member in dead for member in members):
                kept_partial += 1
                continue
            if self._delete_whole_bundle(pack_key, members):
                swept.extend(members)
        if kept_partial:
            log_warning(
                f"[VersionS3] kept {kept_partial} partially-dead bundle(s) "
                f"for project {self._project_id}; dead members remain "
                f"packed until a repack reclaims them.",
            )
        return len(swept), swept

    def _delete_whole_bundle(self, pack_key: str, members: list[str]) -> bool:
        """Delete one fully-dead ``.pob`` and its members' location rows."""
        prefixes = (
            (self._bundle_prefix,)
            if collection_context.get() is not None
            else self._chunk_bundle_prefixes()
        )
        if not any(pack_key.startswith(prefix + "/") for prefix in prefixes):
            raise StorageWriteError("bundle deletion is outside its Project namespace")
        self._authorize_deletion()
        self.invalidate_proofs(members)
        try:
            _run_async(self._physical_s3().delete_file(pack_key))
        except Exception as exc:
            if not _is_not_found_error(exc):
                log_error(f"[VersionS3] delete bundle {pack_key}: {exc}")
                raise  # A timed-out DELETE may still be running remotely.
        self._delete_object_location_rows(members)
        with self._location_lock:
            for member in members:
                self._location_cache.pop(member, None)
        return True

    def _object_ids_in_pack(self, pack_key: str) -> list[str]:
        """All object ids stored in one pack, via the per-pack index."""
        if self._supabase is None:
            return []
        ids: list[str] = []
        page = 1000
        start = 0
        while True:
            resp = (
                self._supabase.client.table(OBJECT_LOCATIONS_TABLE)
                .select("object_id")
                .eq("project_id", self._project_id)
                .eq("pack_key", pack_key)
                .range(start, start + page - 1)
                .execute()
            )
            rows = safe_data(resp) or []
            ids.extend(str(r.get("object_id") or "") for r in rows if r.get("object_id"))
            if len(rows) < page:
                return ids
            start += page

    def invalidate_proofs(self, object_ids: list[str]) -> None:
        """Invalidate ancestors BEFORE deleting bytes; uncertain I/O stays unsafe."""
        if self._supabase is None:
            return
        for offset in range(0, len(object_ids), 200):
            self._supabase.client.rpc(
                "invalidate_version_object_proofs",
                {
                    "p_project_id": self._project_id,
                    "p_oids": object_ids[offset : offset + 200],
                },
            ).execute()

    def _collect_capacity(self, object_ids: list[str]) -> None:
        context = collection_context.get()
        if context is None:
            return
        if context.project_id != self._project_id or self._supabase is None:
            raise StorageWriteError("native capacity collection unavailable")
        response = self._supabase.client.rpc(
            "collect_version_object_capacity",
            {
                "p_project_id": self._project_id,
                "p_gc_token": context.token,
                "p_object_ids": object_ids,
            },
        ).execute()
        if not isinstance(response.data, dict) or any(
            type(response.data.get(key)) is not int or response.data[key] < 0
            for key in ("removed_body_bytes", "removed_objects")
        ):
            raise StorageWriteError("invalid capacity collection result")

    def _delete_object_location_rows(self, object_ids: list[str]) -> None:
        if self._supabase is None or not object_ids:
            return
        for i in range(0, len(object_ids), 100):
            chunk = [oid for oid in object_ids[i : i + 100] if oid]
            if not chunk:
                continue
            try:
                context = collection_context.get()
                if context is not None:
                    if context.project_id != self._project_id:
                        raise StorageWriteError("collection context belongs to another Project")
                    self._supabase.client.rpc(
                        "remove_version_object_locations",
                        {
                            "p_project_id": self._project_id,
                            "p_gc_token": context.token,
                            "p_object_ids": chunk,
                        },
                    ).execute()
                    self._collect_capacity(chunk)
                else:
                    (
                        self._supabase.client.table(OBJECT_LOCATIONS_TABLE)
                        .delete()
                        .eq("project_id", self._project_id)
                        .in_("object_id", chunk)
                        .execute()
                    )
            except Exception as exc:
                log_error(f"[VersionS3] delete location rows: {exc}")
                raise  # A late index DELETE must not race a newly published location.

    # ── Async methods (for direct use in async contexts) ──

    async def async_get(self, h: str) -> bytes:
        location = self._lookup_object_location(h)
        return await self._async_get_with_location(h, location)

    async def _async_get_with_location(self, h: str, location: ObjectLocation | None) -> bytes:
        if location is not None:
            return await self._async_get_packed_object_at(h, location)
        key = self._key_for(h)
        try:
            data = await self._s3.download_file(key)
        except ObjectNotFoundError as exc:
            return await self._async_get_packed_object(h, cause=exc)
        except Exception as e:
            if _is_not_found_error(e):
                return await self._async_get_packed_object(h, cause=e)
            raise
        # Read-side half of the 520885e2 fix: verify the primary-namespace
        # bytes decode to the hash we asked for. Stale pre-Git-protocol
        # payloads (or a half-written object) can squat on a primary loose
        # key; without this guard the caller zlib-decompresses garbage and
        # bulk push dies with "invalid git loose object". On mismatch we
        # treat it exactly like a 404 and fall through to the indexed bundle lookup.
        try:
            _verify_loose_hash(h, data)
        except StorageWriteError as verify_err:
            log_warning(
                f"[VersionS3] stale primary-namespace bytes at {key} "
                f"(hash={h[:12]}): {verify_err}. Falling through to "
                f"indexed bundle lookup.",
            )
            return await self._async_get_packed_object(h, cause=verify_err)
        return data

    async def async_get_range(
        self, h: str, start: int = 0, limit: int | None = None
    ) -> tuple[bytes, int]:
        location = self._lookup_object_location(h)
        if location is not None:
            data = await self._async_get_packed_object_at(h, location)
            end = len(data) if limit is None else min(len(data), start + limit)
            return data[start:end], len(data)
        # Primary loose objects are small; read the whole verified object
        # via ``async_get`` and slice. A partial range read could not be
        # hash-verified (see ``get_range`` for the same rationale).
        data = await self._async_get_with_location(h, location)
        end = len(data) if limit is None else min(len(data), start + limit)
        return data[start:end], len(data)

    async def async_put(self, h: str, data: bytes) -> None:
        self._check_publication_context()
        kind, size = _verify_loose_hash(h, data)
        io_id = await self._async_reserve_capacity(
            [dict(object_id=h, object_kind=kind, body_bytes=size)]
        )
        route = self._active_io_strategy().plan_single(h, len(data))
        if route.layout is ObjectWriteLayout.CHUNKED:
            await self._async_put_chunked_object(h, data)
        else:
            await self._do_put(self._key_for(h), data, expected_hash=h)
        await self._async_settle_capacity(io_id)

    async def async_exists(self, h: str) -> bool:
        if self._lookup_object_location(h) is not None:
            return True
        try:
            if await self._s3.file_exists(self._key_for(h)):
                return True
            return self._lookup_object_location(h) is not None
        except Exception as exc:
            if _is_not_found_error(exc):
                return self._lookup_object_location(h) is not None
            raise

    async def async_get_many(self, hashes: list[str], concurrency: int = 20) -> dict[str, bytes]:
        """Fetch multiple objects in parallel. Returns {hash: bytes}."""
        import asyncio

        unique = list(dict.fromkeys(hashes))
        locations = {h: self._cached_object_location(h) for h in unique}
        if hasattr(self, "_pinned_locations"):
            locations = {h: self._pinned_locations.get(h) for h in unique}
        elif self._supabase is not None:
            remaining = [h for h in unique if locations[h] is None]
            if remaining:
                locations.update(
                    await asyncio.to_thread(self._lookup_many_object_locations, remaining)
                )

        sem = asyncio.Semaphore(concurrency)
        results: dict[str, bytes] = {}

        async def _fetch(h: str):
            async with sem:
                results[h] = await self._async_get_with_location(h, locations[h])

        await asyncio.gather(*[_fetch(h) for h in unique])
        return results

    async def async_put_many(
        self, objects: dict[str, bytes], concurrency: int = 20, skip_exists: bool = False
    ) -> None:
        """Upload multiple objects in parallel.

        Args:
            skip_exists: If True, skip the HEAD existence check before PUT.
                Use when the caller already knows these objects don't exist
                (e.g. negotiate confirmed them as missing).
        """
        import asyncio

        self._check_publication_context()
        facts = []
        for object_id, data in objects.items():
            kind, size = _verify_loose_hash(object_id, data)
            facts.append(dict(object_id=object_id, object_kind=kind, body_bytes=size))
        io_id = await self._async_reserve_capacity(facts)
        plan = self._active_io_strategy().plan_batch(
            {object_id: len(data) for object_id, data in objects.items()}
        )
        if plan.uses_location_index:
            await self._async_put_bundled_or_chunked(objects)
            await self._async_settle_capacity(io_id)
            return

        sem = asyncio.Semaphore(concurrency)

        async def _upload(h: str, data: bytes):
            async with sem:
                route = plan.route_for(h)
                if route.layout is ObjectWriteLayout.CHUNKED:
                    await self._async_put_chunked_object(h, data)
                    return
                key = self._key_for(h)
                if skip_exists:
                    await self._physical_s3().upload_file(
                        key, data, content_type="application/octet-stream"
                    )
                else:
                    await self._do_put(key, data, expected_hash=h)

        with trace_phase(
            "s3.put_many",
            count=len(objects),
            bytes=sum(len(data) for data in objects.values()),
            skip_exists=skip_exists,
        ):
            results = await asyncio.gather(
                *[_upload(h, d) for h, d in objects.items()],
                return_exceptions=True,
            )
        errors = [item for item in results if isinstance(item, BaseException)]
        if errors:
            raise errors[0]
        await self._async_settle_capacity(io_id)

    async def _async_reserve_capacity(self, facts: list[dict]) -> str | None:
        import uuid

        context = publication_context.get()
        if context is None or not facts:
            return None
        io_id, claimed = str(uuid.uuid4()), False
        if self._supabase is None:
            raise StorageWriteError("native capacity admission unavailable")
        # Before ANY loose, bundle, part or manifest PUT. Encoded bytes and
        # compression/chunk boundaries never define logical object identity.
        for offset in range(0, len(facts), 200):
            batch = facts[offset : offset + 200]
            try:
                response = await asyncio.to_thread(
                    lambda batch=batch: self._supabase.client.rpc(
                        "reserve_version_object_capacity",
                        {
                            "p_project_id": self._project_id,
                            "p_actor": context.actor,
                            "p_pin_id": context.pin_id,
                            "p_objects": batch,
                            "p_required": context.require_capacity,
                            "p_io_id": io_id,
                        },
                    ).execute()
                )
                if not isinstance(response.data, dict) or any(
                    type(response.data.get(key)) is not int or response.data[key] < 0
                    for key in ("new_body_bytes", "new_objects")
                ):
                    raise RuntimeError("invalid capacity admission result")
                dormant = response.data.get("profile") == "dormant"
                if context.require_capacity and dormant:
                    raise RuntimeError("invalid capacity admission result")
                claimed = claimed or not dormant
            except Exception as exc:
                raise StorageWriteError(f"native capacity admission failed: {exc}") from exc
        return io_id if claimed else None

    async def _async_settle_capacity(self, io_id: str | None) -> None:
        if io_id is None:
            return
        context = publication_context.get()
        if context is None or self._supabase is None:
            raise StorageWriteError("native capacity settlement unavailable")
        # Only after ALL physical/index work returned successfully. No finally
        # cleanup on cancellation/timeout: remote I/O may still be outstanding.
        # Another invocation (even on this same pin) cannot settle this identity.
        try:
            response = await asyncio.to_thread(
                lambda: self._supabase.client.rpc(
                    "settle_version_object_capacity_io",
                    {
                        "p_project_id": self._project_id,
                        "p_actor": context.actor,
                        "p_pin_id": context.pin_id,
                        "p_io_id": io_id,
                    },
                ).execute()
            )
            if (
                not isinstance(response.data, dict)
                or type(response.data.get("settled_objects")) is not int
                or response.data["settled_objects"] < 0
            ):
                raise RuntimeError("invalid capacity settlement result")
        except Exception as exc:
            raise StorageWriteError(f"native capacity settlement failed: {exc}") from exc

    def _active_io_strategy(self) -> IOStorageStrategy:
        if self._supabase is not None:
            return self._io_strategy
        return self._io_strategy.without_location_index()

    def _get_packed_object(self, h: str, cause: Exception | None = None) -> bytes:
        location = self._lookup_object_location(h)
        if location is None:
            raise ObjectNotFoundError(f"object not found in S3: {h}") from cause
        return self._get_packed_object_at(h, location)

    def _get_packed_object_at(self, h: str, location: ObjectLocation) -> bytes:
        self._validate_location(location)
        if location.pack_key.startswith(_CHUNKED_PACK_PREFIX):
            return _run_async(self._async_get_chunked_object_at(h, location))
        try:
            with trace_phase(
                "s3.pack.get",
                object_id=h[:12],
                size_bytes=location.size_bytes,
            ):
                data, _total = _run_async(
                    self._s3.download_file_range(
                        location.pack_key,
                        start=location.offset_bytes,
                        limit=location.size_bytes,
                    )
                )
            _verify_loose_hash(h, data)
            return data
        except Exception as exc:
            if _is_not_found_error(exc):
                raise ObjectNotFoundError(
                    f"packed object not found in S3: {h}",
                ) from exc
            raise

    async def _async_get_packed_object(
        self,
        h: str,
        cause: Exception | None = None,
    ) -> bytes:
        location = self._lookup_object_location(h)
        if location is None:
            raise ObjectNotFoundError(f"object not found in S3: {h}") from cause
        return await self._async_get_packed_object_at(h, location)

    async def _async_get_packed_object_at(
        self,
        h: str,
        location: ObjectLocation,
    ) -> bytes:
        self._validate_location(location)
        if location.pack_key.startswith(_CHUNKED_PACK_PREFIX):
            return await self._async_get_chunked_object_at(h, location)
        try:
            with trace_phase(
                "s3.pack.get",
                object_id=h[:12],
                size_bytes=location.size_bytes,
            ):
                data, _total = await self._s3.download_file_range(
                    location.pack_key,
                    start=location.offset_bytes,
                    limit=location.size_bytes,
                )
            _verify_loose_hash(h, data)
            return data
        except Exception as exc:
            if _is_not_found_error(exc):
                raise ObjectNotFoundError(
                    f"packed object not found in S3: {h}",
                ) from exc
            raise

    async def _async_get_chunked_object_at(
        self,
        h: str,
        location: ObjectLocation,
    ) -> bytes:
        manifest_key = location.pack_key.removeprefix(_CHUNKED_PACK_PREFIX)
        prefixes = self._chunk_bundle_prefixes()
        chunk_manifest_root(manifest_key, h, prefixes)
        try:
            with trace_phase(
                "s3.chunked.get",
                object_id=h[:12],
                size_bytes=location.size_bytes,
            ):
                manifest_raw = await self._s3.download_file(manifest_key)
                immutable, ordered_chunks = validate_chunk_manifest(
                    manifest_raw,
                    key=manifest_key,
                    oid=h,
                    size=location.size_bytes,
                    bundle_prefixes=prefixes,
                    require_immutable=self._require_immutable_chunks,
                )
                sem = asyncio.Semaphore(_OBJECT_UPLOAD_CONCURRENCY)

                async def download_one(chunk: dict) -> tuple[int, bytes]:
                    key = str(chunk.get("key") or "")
                    offset = int(chunk.get("offset_bytes") or 0)
                    expected_size = int(chunk.get("size_bytes") or 0)
                    async with sem:
                        part = await self._s3.download_file(key)
                    if len(part) != expected_size:
                        raise StorageWriteError(
                            f"chunk size mismatch for {h}: {key}",
                        )
                    if immutable and hashlib.sha256(part).hexdigest() != key.rsplit("part-", 1)[1]:
                        raise StorageWriteError(f"chunk digest mismatch for {h}: {key}")
                    return offset, part

                fetched_parts = await asyncio.gather(
                    *[download_one(chunk) for chunk in ordered_chunks],
                )
                parts = [
                    part
                    for _offset, part in sorted(
                        fetched_parts,
                        key=lambda item: item[0],
                    )
                ]
                data = b"".join(parts)
            if len(data) != location.size_bytes:
                raise StorageWriteError(f"chunked object size mismatch for {h}")
            _verify_loose_hash(h, data)
            return data
        except Exception as exc:
            if _is_not_found_error(exc):
                raise ObjectNotFoundError(
                    f"chunked object not found in S3: {h}",
                ) from exc
            raise

    def _validate_location(self, location: ObjectLocation) -> None:
        key = location.pack_key.removeprefix(_CHUNKED_PACK_PREFIX)
        if (
            not key.startswith(self._bundle_prefix + "/")
            or "/../" in key
            or "\\" in key
            or location.offset_bytes < 0
            or location.size_bytes <= 0
        ):
            raise StorageWriteError("object location is outside its canonical Project namespace")

    def _lookup_object_location(self, h: str) -> ObjectLocation | None:
        if hasattr(self, "_pinned_locations"):
            return self._pinned_locations.get(h)
        cached = self._cached_object_location(h)
        if cached is not None:
            return cached
        found = self._lookup_many_object_locations([h])
        return found.get(h)

    def _cached_object_location(self, h: str) -> ObjectLocation | None:
        with self._location_lock:
            cached = self._location_cache.get(h)
        if cached is not None:
            trace_mark("object.pack.index.hit", object_id=h[:12])
            return cached
        return None

    def _lookup_many_object_locations(self, hashes: list[str]) -> dict[str, ObjectLocation]:
        if self._supabase is None:
            return {}

        found: dict[str, ObjectLocation] = {}
        for i in range(0, len(hashes), 100):
            chunk = hashes[i : i + 100]
            if not chunk:
                continue
            with trace_phase("db.object_location.lookup", count=len(chunk)):
                resp = (
                    self._supabase.client.table(OBJECT_LOCATIONS_TABLE)
                    .select("object_id, pack_key, offset_bytes, size_bytes")
                    .eq("project_id", self._project_id)
                    .in_("object_id", chunk)
                    .execute()
                )
            rows = safe_data(resp) or []
            for row in rows:
                object_id = str(row.get("object_id") or "")
                location = ObjectLocation(
                    pack_key=str(row.get("pack_key") or ""),
                    offset_bytes=int(row.get("offset_bytes") or 0),
                    size_bytes=int(row.get("size_bytes") or 0),
                )
                if object_id and location.pack_key and location.size_bytes > 0:
                    self._validate_location(location)
                    found[object_id] = location
        with self._location_lock:
            self._location_cache.update(found)
        return found

    async def _async_put_bundle(self, objects: dict[str, bytes]) -> None:
        uploads, rows = self._bundle_upload_plan(objects)
        with trace_phase(
            "s3.pack.put",
            count=len(objects),
            bytes=sum(len(data) for _key, data, _content_type in uploads),
        ):
            await self._async_upload_physical_objects(uploads)
        await self._async_verify_physical_uploads(uploads)
        await self._async_upsert_object_locations(rows)

    async def _async_put_bundled_or_chunked(self, objects: dict[str, bytes]) -> None:
        strategy = self._active_io_strategy()
        pending: dict[str, bytes] = {}
        pending_size = 0
        uploads: list[tuple[str, bytes, str]] = []
        rows: list[dict] = []

        async def flush_pending() -> None:
            nonlocal pending, pending_size
            if not pending:
                return
            bundle_uploads, bundle_rows = self._bundle_upload_plan(pending)
            uploads.extend(bundle_uploads)
            rows.extend(bundle_rows)
            pending = {}
            pending_size = 0

        for object_id, data in sorted(objects.items()):
            if len(data) > strategy.bundle_target_bytes:
                await flush_pending()
                chunk_uploads, chunk_row = self._chunked_object_upload_plan(object_id, data)
                uploads.extend(chunk_uploads)
                rows.append(chunk_row)
                continue
            if pending and pending_size + len(data) > strategy.bundle_target_bytes:
                await flush_pending()
            pending[object_id] = data
            pending_size += len(data)
        await flush_pending()

        with trace_phase(
            "s3.pack.batch_put",
            object_count=len(objects),
            physical_count=len(uploads),
            bytes=sum(len(data) for _key, data, _content_type in uploads),
        ):
            await self._async_upload_physical_objects(uploads)
        await self._async_verify_physical_uploads(uploads)
        await self._async_upsert_object_locations(rows)

    async def _async_put_chunked_object(self, h: str, data: bytes) -> None:
        uploads, row = self._chunked_object_upload_plan(h, data)
        with trace_phase(
            "s3.chunked.put",
            object_id=h[:12],
            size_bytes=len(data),
            physical_count=len(uploads),
        ):
            await self._async_upload_physical_objects(uploads)
        await self._async_verify_physical_uploads(uploads)
        await self._async_upsert_object_locations([row])

    def _bundle_upload_plan(
        self,
        objects: dict[str, bytes],
    ) -> tuple[list[tuple[str, bytes, str]], list[dict]]:
        bundle, entries = _encode_object_bundle(objects)
        pack_key = self._bundle_key_for(bundle)
        rows = [
            {
                "project_id": self._project_id,
                "object_id": entry["object_id"],
                "pack_key": pack_key,
                "offset_bytes": entry["offset_bytes"],
                "size_bytes": entry["size_bytes"],
            }
            for entry in entries
        ]
        return [(pack_key, bundle, "application/octet-stream")], rows

    def _chunked_object_upload_plan(
        self,
        h: str,
        data: bytes,
    ) -> tuple[list[tuple[str, bytes, str]], dict]:
        uploads, manifest_key = chunk_upload_plan(
            self._bundle_prefix,
            h,
            data,
            self._active_io_strategy().chunk_bytes,
        )
        row = {
            "project_id": self._project_id,
            "object_id": h,
            "pack_key": f"{_CHUNKED_PACK_PREFIX}{manifest_key}",
            "offset_bytes": 0,
            "size_bytes": len(data),
        }
        return uploads, row

    async def _async_upload_physical_objects(
        self,
        uploads: list[tuple[str, bytes, str]],
    ) -> None:
        sem = asyncio.Semaphore(_OBJECT_UPLOAD_CONCURRENCY)

        async def upload_one(key: str, data: bytes, content_type: str) -> None:
            async with sem:
                await self._physical_s3().upload_file(
                    key,
                    data,
                    content_type=content_type,
                )

        results = await asyncio.gather(
            *[upload_one(key, data, content_type) for key, data, content_type in uploads],
            return_exceptions=True,
        )
        errors = [item for item in results if isinstance(item, BaseException)]
        if errors:
            raise errors[0]

    async def _async_verify_physical_uploads(self, uploads: list[tuple[str, bytes, str]]) -> None:
        """Prove replacement bytes BEFORE changing any canonical location.

        A final ref closure check is too late: an unverified replacement index
        can already have broken an older acknowledged object. Read each physical
        bundle/part once, not once per member. Publication/GC epoch fencing is
        additionally required to order delayed index requests against deletion.
        """
        sem = asyncio.Semaphore(_OBJECT_UPLOAD_CONCURRENCY)

        async def verify(key: str, expected: bytes) -> None:
            try:
                async with sem:
                    actual = await self._s3.download_file(key)
            except Exception as exc:
                raise StorageWriteError(f"physical upload verification failed: {key}") from exc
            if actual != expected:
                raise StorageWriteError(f"physical upload verification failed: {key}")

        with trace_phase("s3.verify_uploads", count=len(uploads)):
            await asyncio.gather(*(verify(key, data) for key, data, _content_type in uploads))

    async def _async_upsert_object_locations(self, rows: list[dict]) -> None:
        if not rows:
            return
        with trace_phase("db.object_location.upsert", count=len(rows)):
            for offset in range(0, len(rows), _OBJECT_LOCATION_UPSERT_BATCH_SIZE):
                chunk = rows[offset : offset + _OBJECT_LOCATION_UPSERT_BATCH_SIZE]
                context = publication_context.get()
                if context is not None:
                    if context.project_id != self._project_id:
                        raise StorageWriteError("publication context belongs to another Project")
                    try:
                        await asyncio.to_thread(
                            lambda batch=chunk, context=context: self._supabase.client.rpc(
                                "register_version_object_locations",
                                {
                                    "p_project_id": self._project_id,
                                    "p_actor": context.actor,
                                    "p_pin_id": context.pin_id,
                                    "p_rows": batch,
                                },
                            ).execute()
                        )
                    except Exception as exc:
                        # Never fall back to an unfenced table upsert, including
                        # missing schema capability and late/expired pins.
                        raise StorageWriteError(
                            f"native location registration failed: {exc}"
                        ) from exc
                else:
                    await asyncio.to_thread(
                        lambda batch=chunk: (
                            self._supabase.client.table(OBJECT_LOCATIONS_TABLE)
                            .upsert(
                                batch,
                                on_conflict="project_id,object_id",
                            )
                            .execute()
                        )
                    )
        with self._location_lock:
            for row in rows:
                self._location_cache[row["object_id"]] = ObjectLocation(
                    pack_key=row["pack_key"],
                    offset_bytes=int(row["offset_bytes"]),
                    size_bytes=int(row["size_bytes"]),
                )

    async def async_exists_many(
        self,
        hashes: list[str],
        concurrency: int = 20,
        *,
        check_packed_locations: bool = True,
    ) -> set[str]:
        """Check existence of multiple objects in parallel. Returns set of existing hashes."""
        import asyncio

        sem = asyncio.Semaphore(concurrency)
        existing: set[str] = set()

        async def _check(h: str):
            async with sem:
                if check_packed_locations:
                    exists = await self.async_exists(h)
                else:
                    exists = await self._s3.file_exists(self._key_for(h))
                if exists:
                    existing.add(h)

        # Do not use ``return_exceptions=True`` here. A transient Supabase/S3
        # failure is not the same thing as "object is missing"; swallowing the
        # exception makes callers mark healthy tree entries as ``damaged`` until
        # the next refresh happens to succeed.
        await asyncio.gather(*[_check(h) for h in hashes])
        return existing

    async def _do_put(self, key: str, data: bytes, expected_hash: str | None = None) -> None:
        """Write ``data`` at ``key``, skipping the PUT when the object is
        already present (content-addressed objects are immutable, so an
        existing key normally means "already have it").

        Hash-on-write self-heal (runbook bulk-push-520885e2 §8②): when
        ``expected_hash`` is supplied AND a key already exists, we verify
        the bytes ALREADY there decode to that hash. If they don't — i.e.
        a stale pre-Git-protocol payload or a half-written object is
        squatting on this key — we OVERWRITE with the correct bytes
        instead of trusting the dedup skip. This is the write-side half
        of the 520885e2 fix: it means re-uploading the same file to a
        project that has corrupt bytes under its key self-heals on the
        next push, with no ops intervention. The read-side half lives in
        ``get`` / ``async_get`` (the read paths verify primary bytes and
        fall through corrupt ones to the indexed bundle lookup).

        ``expected_hash`` is omitted only by callers that genuinely don't
        know it (none today); when omitted we keep the legacy skip-if-
        exists behaviour.
        """
        if await self._s3.file_exists(key):
            if expected_hash is None:
                return
            # Key exists — confirm the resident bytes are the object we
            # think they are before trusting the dedup skip.
            try:
                existing = await self._s3.download_file(key)
                _verify_loose_hash(expected_hash, existing)
                return  # resident bytes are valid; dedup skip stands.
            except StorageWriteError:
                log_warning(
                    f"[VersionS3] hash-on-write: stale/corrupt bytes at {key} "
                    f"(expected {expected_hash[:12]}); overwriting with correct object",
                )
            except Exception as exc:
                if not _is_not_found_error(exc):
                    log_warning(
                        f"[VersionS3] hash-on-write: could not verify resident "
                        f"bytes at {key} ({exc}); overwriting",
                    )
        await self._physical_s3().upload_file(key, data, content_type="application/octet-stream")

    async def async_scan_primary_loose_integrity(
        self,
        *,
        hard_cap: int = 1_000_000,
        heal: bool = False,
    ) -> dict:
        """Sweep this project's primary loose-object prefix, verify each
        object's bytes, and report (optionally delete) corrupt ones.

        This is the engine half of the runbook §8① background integrity
        scan. It pages through the primary objects prefix, downloads each
        loose object, and runs ``_verify_loose_hash``. Corrupt entries
        (the 520885e2 class — stale pre-Git-protocol bytes squatting on a
        loose key) are collected; when ``heal=True`` they're deleted so a
        subsequent re-upload writes the correct bytes via the normal PUT
        path.

        Returns a summary dict; never raises for a single bad object — a
        scan must survive individual failures to be useful.
        """
        if not hasattr(self._s3, "list_files"):
            return {"supported": False, "checked": 0, "corrupt": [], "healed": 0}

        prefix = f"{self._layout.object_prefix}/"
        checked = 0
        corrupt: list[str] = []
        healed = 0
        truncated = False
        continuation: str | None = None
        while True:
            files, _prefixes, next_token, is_truncated = await self._s3.list_files(
                prefix=prefix,
                max_keys=1000,
                continuation_token=continuation,
            )
            for item in files:
                h = _hash_from_loose_key(item.key, prefix)
                if h is None:
                    continue
                checked += 1
                verdict = await self._diagnose_loose_key(item.key, h, heal=heal)
                if verdict == "corrupt":
                    corrupt.append(h)
                elif verdict == "healed":
                    corrupt.append(h)
                    healed += 1
                if checked >= hard_cap:
                    truncated = True
                    break
            if truncated or not is_truncated or not next_token:
                break
            continuation = next_token

        return {
            "supported": True,
            "checked": checked,
            "corrupt": corrupt,
            "healed": healed,
            "truncated": truncated,
        }

    async def _diagnose_loose_key(self, key: str, h: str, *, heal: bool) -> str:
        """Verify one primary loose key. Returns ``"ok"`` /
        ``"corrupt"`` / ``"healed"`` / ``"skip"``."""
        try:
            data = await self._s3.download_file(key)
            _verify_loose_hash(h, data)
            return "ok"
        except StorageWriteError:
            # A known-bad object is no longer a trusted closure boundary,
            # including when this audit is read-only. Fail closed if fencing
            # the dependent proofs fails; never delete first.
            await asyncio.to_thread(self.invalidate_proofs, [h])
            if not heal:
                return "corrupt"
            try:
                await self._physical_s3().delete_file(key)
                return "healed"
            except Exception as exc:
                log_warning(f"[integrity-scan] heal delete failed for {key}: {exc}")
                return "corrupt"
        except Exception as exc:
            # Unreadable (transient S3 error / vanished key) — skip, not corrupt.
            if not _is_not_found_error(exc):
                log_warning(f"[integrity-scan] could not read {key}: {exc}")
            return "skip"


def _is_not_found_error(exc: Exception) -> bool:
    """Detect S3 'object not found' errors across exception wrapper types."""
    msg = str(exc).lower()
    return any(s in msg for s in ("not found", "nosuchkey", "404", "does not exist"))


def _hash_from_loose_key(key: str, prefix: str) -> str | None:
    """Reconstruct the 40-hex object id from a loose S3 key shaped
    ``{prefix}{shard2}/{rest38}``. Returns ``None`` for keys that don't
    fit the loose layout (e.g. bundle / chunk / manifest keys)."""
    suffix = key[len(prefix) :]
    parts = suffix.split("/", 1)
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 38:
        return parts[0] + parts[1]
    return None


class _S3DurableReadback:
    """One closure proof's bounded location window, with fresh physical reads.

    Callers retain the publication pin. No object bytes or metadata from an older
    proof/cache are reused. Consume locations once; never load the project index.
    """

    def __init__(self, backend):
        self.reader = S3StorageBackend(
            backend._s3,
            backend._project_id,
            supabase=backend._supabase,
            io_strategy=backend._io_strategy,
            require_immutable_chunks=True,
        )
        self.pending = {}
        self.verified_bytes = {}
        self.buffered_bytes = 0

    def prefetch_durable(self, hashes):
        available = 100 - len(self.pending)
        wanted = list(dict.fromkeys(h for h in hashes if h not in self.pending))[:available]
        if not wanted:
            return
        locations = self.reader._lookup_many_object_locations(wanted)
        for location in locations.values():
            key = location.pack_key.removeprefix(_CHUNKED_PACK_PREFIX)
            if not key.startswith(self.reader._bundle_prefix + "/"):
                raise StorageWriteError(
                    "publication object location outside canonical Project namespace"
                )
        self.pending.update((oid, locations.get(oid)) for oid in wanted)
        self.reader._location_cache.clear()
        # Fresh physical readback, scoped to this proof. Adjacent small objects
        # in one immutable container share a range GET; no whole-project cache.
        groups = {}
        for oid in wanted:
            location = locations.get(oid)
            if location is not None and not location.pack_key.startswith(_CHUNKED_PACK_PREFIX):
                self.reader._validate_location(location)
                groups.setdefault(location.pack_key, []).append((oid, location))
        ranges = []
        for key, entries in groups.items():
            members, required = [], 0
            for oid, location in sorted(entries, key=lambda item: item[1].offset_bytes):
                end = location.offset_bytes + location.size_bytes
                if members and end - members[0][1].offset_bytes > min(
                    2 * (required + location.size_bytes), 8 * 1024**2
                ):
                    ranges.append((key, members))
                    members, required = [], 0
                members.append((oid, location))
                required += location.size_bytes
            if members:
                ranges.append((key, members))
        for key, members in ranges:
            start = min(location.offset_bytes for _, location in members)
            end = max(location.offset_bytes + location.size_bytes for _, location in members)
            required = sum(location.size_bytes for _, location in members)
            if len(members) < 2 or end - start > min(
                2 * required, 8 * 1024**2 - self.buffered_bytes
            ):
                continue
            data, _ = _run_async(
                self.reader._s3.download_file_range(key, start=start, limit=end - start)
            )
            if len(data) != end - start:
                raise StorageWriteError("incomplete publication container readback")
            for oid, location in members:
                offset = location.offset_bytes - start
                loose = data[offset : offset + location.size_bytes]
                _verify_loose_hash(oid, loose)
                self.verified_bytes[oid] = loose
                self.buffered_bytes += len(loose)

    def get_durable(self, oid):
        if oid in self.verified_bytes:
            data = self.verified_bytes.pop(oid)
            self.buffered_bytes -= len(data)
            self.pending.pop(oid, None)
            return data
        if oid not in self.pending:
            return self.reader.get_durable(oid)
        return self.reader._get_with_location(oid, self.pending.pop(oid))
