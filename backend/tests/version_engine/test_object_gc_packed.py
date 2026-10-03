"""GAP-2 regression tests: object GC must see and sweep packed objects.

Historically ``S3StorageBackend.all_hashes()`` listed only the loose
``objects/`` prefix, so bundled (.pob) and chunked objects were invisible
to GC and batch-written orphans accumulated forever. These tests lock in:

  - enumeration unions loose + packed (from the location index),
  - ``delete`` routes by layout: loose deletes its key, chunked deletes
    manifest + parts, bundled is refused (can't break a shared pack),
  - ``sweep_dead_bundles`` drops a fully-dead bundle but keeps a
    partially-dead one.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.storage.backends.s3 import (
    S3StorageBackend,
    _CHUNKED_PACK_PREFIX,
)


# ── Fakes ───────────────────────────────────────────────────────────


class FakeS3:
    """Minimal async S3 service: an in-memory key→bytes store."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def download_file(self, key: str) -> bytes:
        if key not in self.objects:
            raise RuntimeError(f"not found: {key}")
        return self.objects[key]

    async def delete_file(self, key: str) -> None:
        if key not in self.objects:
            raise RuntimeError(f"not found: {key}")
        del self.objects[key]
        self.deleted.append(key)

    async def file_exists(self, key: str) -> bool:
        return key in self.objects

    async def list_files(self, prefix="", max_keys=1000, continuation_token=None):
        items = [
            SimpleNamespace(key=key, size=len(data), last_modified="2026-01-01T00:00:00+00:00")
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]
        return items, [], None, False


class FakeQuery:
    def __init__(self, store: "FakeSupabaseTables", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._eq: dict[str, str] = {}
        self._in: tuple[str, set] | None = None
        self._range: tuple[int, int] | None = None

    def select(self, _cols):
        self._op = "select"
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._eq[col] = str(val)
        return self

    def in_(self, col, vals):
        self._in = (col, set(vals))
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def _match(self, row: dict) -> bool:
        for col, val in self._eq.items():
            if str(row.get(col)) != val:
                return False
        if self._in is not None:
            col, vals = self._in
            if row.get(col) not in vals:
                return False
        return True

    def execute(self):
        rows = self._store.tables.get(self._table, [])
        matched = [r for r in rows if self._match(r)]
        if self._op == "delete":
            self._store.tables[self._table] = [r for r in rows if not self._match(r)]
            return SimpleNamespace(data=matched)
        if self._range is not None:
            start, end = self._range
            matched = matched[start:end + 1]
        return SimpleNamespace(data=matched)


class FakeSupabaseTables:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, name, args):
        # These are legacy layout component tests, not native admission proof.
        assert name == "authorize_version_object_deletion"
        assert args == {"p_project_id": PROJECT, "p_gc_token": None}
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=True))


class FakeSupabaseClient:
    def __init__(self) -> None:
        self.client = FakeSupabaseTables()


# ── Helpers ─────────────────────────────────────────────────────────


OID_LOOSE = "a" * 40
OID_CHUNKED = "b" * 40
OID_BUNDLE_1 = "c" * 40
OID_BUNDLE_2 = "d" * 40

_NS = "version"  # canonical namespace
PROJECT = "proj1"


def _loose_key(oid: str) -> str:
    return f"{_NS}/{PROJECT}/objects/{oid[:2]}/{oid[2:]}"


def _bundle_prefix() -> str:
    return f"{_NS}/{PROJECT}/object-bundles"


def _chunk_manifest_key(oid: str) -> str:
    return f"{_bundle_prefix()}/chunked/{oid[:2]}/{oid}.json"


def _make_backend():
    s3 = FakeS3()
    supa = FakeSupabaseClient()
    backend = S3StorageBackend(
        s3, PROJECT, supabase=supa, allow_deferred_namespace_reads=False,
    )
    return backend, s3, supa


def _add_location_row(supa, oid, pack_key, size=10, created_at="2026-01-01T00:00:00+00:00"):
    supa.client.tables.setdefault("version_object_locations", []).append({
        "project_id": PROJECT,
        "object_id": oid,
        "pack_key": pack_key,
        "offset_bytes": 0,
        "size_bytes": size,
        "created_at": created_at,
    })


# ── Tests ───────────────────────────────────────────────────────────


def test_all_hashes_unions_loose_and_packed():
    backend, s3, supa = _make_backend()
    # one loose object on S3
    s3.objects[_loose_key(OID_LOOSE)] = b"loose"
    # one chunked + two bundled, only in the location index
    _add_location_row(supa, OID_CHUNKED, f"{_CHUNKED_PACK_PREFIX}{_chunk_manifest_key(OID_CHUNKED)}")
    _add_location_row(supa, OID_BUNDLE_1, f"{_bundle_prefix()}/cc/bundle1.pob")
    _add_location_row(supa, OID_BUNDLE_2, f"{_bundle_prefix()}/cc/bundle1.pob")

    hashes = set(backend.all_hashes())
    assert OID_LOOSE in hashes
    assert OID_CHUNKED in hashes
    assert OID_BUNDLE_1 in hashes
    assert OID_BUNDLE_2 in hashes


def test_all_hashes_with_metadata_includes_packed_age():
    backend, _s3, supa = _make_backend()
    _add_location_row(supa, OID_BUNDLE_1, f"{_bundle_prefix()}/cc/b.pob",
                      created_at="2025-12-31T00:00:00+00:00")
    meta = backend.all_hashes_with_metadata()
    assert OID_BUNDLE_1 in meta
    assert meta[OID_BUNDLE_1]["last_modified"] == "2025-12-31T00:00:00+00:00"


def test_no_supabase_falls_back_to_loose_only():
    s3 = FakeS3()
    backend = S3StorageBackend(s3, PROJECT, supabase=None, allow_deferred_namespace_reads=False)
    s3.objects[_loose_key(OID_LOOSE)] = b"loose"
    assert backend.all_hashes() == [OID_LOOSE]


def test_count_sums_loose_and_packed_bytes():
    # GAP-15: count() used to return total_bytes=0. It now sums real sizes
    # across loose (S3 listing) and packed (location index) objects.
    backend, s3, supa = _make_backend()
    s3.objects[_loose_key(OID_LOOSE)] = b"12345"           # 5 bytes loose
    _add_location_row(supa, OID_BUNDLE_1, f"{_bundle_prefix()}/cc/b.pob", size=100)
    _add_location_row(supa, OID_CHUNKED,
                      f"{_CHUNKED_PACK_PREFIX}{_chunk_manifest_key(OID_CHUNKED)}", size=2000)

    n, total = backend.count()
    assert n == 3
    assert total == 5 + 100 + 2000


def test_delete_loose_object_removes_key():
    backend, s3, _supa = _make_backend()
    key = _loose_key(OID_LOOSE)
    s3.objects[key] = b"loose"
    assert backend.delete(OID_LOOSE) is True
    assert key not in s3.objects


def test_delete_chunked_removes_manifest_and_parts():
    backend, s3, supa = _make_backend()
    manifest_key = _chunk_manifest_key(OID_CHUNKED)
    part_key = f"{_bundle_prefix()}/chunked/{OID_CHUNKED[:2]}/{OID_CHUNKED}/part-000001"
    s3.objects[manifest_key] = json.dumps({
        "version": 1, "object_id": OID_CHUNKED, "size_bytes": 5,
        "chunks": [{"key": part_key, "offset_bytes": 0, "size_bytes": 5}],
    }).encode()
    s3.objects[part_key] = b"hello"
    _add_location_row(supa, OID_CHUNKED, f"{_CHUNKED_PACK_PREFIX}{manifest_key}", size=5)

    assert backend.delete(OID_CHUNKED) is True
    assert manifest_key not in s3.objects
    assert part_key not in s3.objects
    # location row purged
    assert supa.client.tables["version_object_locations"] == []


def test_foreign_bundle_location_cannot_authorize_deletion():
    backend, s3, supa = _make_backend()
    foreign = "version/other-project/object-bundles/ff/foreign.pob"
    s3.objects[foreign] = b"another Project's bytes"
    _add_location_row(supa, OID_BUNDLE_1, foreign)
    with pytest.raises(StorageWriteError, match="outside its Project namespace"):
        backend.sweep_dead_bundles({OID_BUNDLE_1})
    assert not s3.deleted and s3.objects[foreign] == b"another Project's bytes"
    assert len(supa.client.tables["version_object_locations"]) == 1


def test_delete_bundled_object_is_refused():
    backend, s3, supa = _make_backend()
    pack_key = f"{_bundle_prefix()}/cc/bundle1.pob"
    s3.objects[pack_key] = b"bundlebytes"
    _add_location_row(supa, OID_BUNDLE_1, pack_key)
    _add_location_row(supa, OID_BUNDLE_2, pack_key)

    # a single bundled object cannot be deleted without repacking
    assert backend.delete(OID_BUNDLE_1) is False
    assert pack_key in s3.objects  # bundle untouched
    # both location rows intact
    assert len(supa.client.tables["version_object_locations"]) == 2


def test_sweep_dead_bundles_deletes_fully_dead_bundle():
    backend, s3, supa = _make_backend()
    pack_key = f"{_bundle_prefix()}/cc/bundle1.pob"
    s3.objects[pack_key] = b"bundlebytes"
    _add_location_row(supa, OID_BUNDLE_1, pack_key)
    _add_location_row(supa, OID_BUNDLE_2, pack_key)

    # both members dead -> whole bundle swept
    count, swept = backend.sweep_dead_bundles({OID_BUNDLE_1, OID_BUNDLE_2})
    assert count == 2
    assert set(swept) == {OID_BUNDLE_1, OID_BUNDLE_2}
    assert pack_key not in s3.objects
    assert supa.client.tables["version_object_locations"] == []


def test_sweep_keeps_partially_dead_bundle():
    backend, s3, supa = _make_backend()
    pack_key = f"{_bundle_prefix()}/cc/bundle1.pob"
    s3.objects[pack_key] = b"bundlebytes"
    _add_location_row(supa, OID_BUNDLE_1, pack_key)
    _add_location_row(supa, OID_BUNDLE_2, pack_key)

    # only one of two members is dead -> bundle MUST be kept (the live
    # member shares the pack)
    count, swept = backend.sweep_dead_bundles({OID_BUNDLE_1})
    assert count == 0
    assert swept == []
    assert pack_key in s3.objects
    assert len(supa.client.tables["version_object_locations"]) == 2


def test_sweep_ignores_chunked_objects():
    backend, _s3, supa = _make_backend()
    manifest_key = _chunk_manifest_key(OID_CHUNKED)
    _add_location_row(supa, OID_CHUNKED, f"{_CHUNKED_PACK_PREFIX}{manifest_key}")
    # chunked is not a .pob; sweep should not touch it
    count, swept = backend.sweep_dead_bundles({OID_CHUNKED})
    assert count == 0
    assert swept == []


# ── GC wiring ───────────────────────────────────────────────────────


def test_delete_eligible_uses_bundle_sweep_then_per_object():
    """The GC delete phase must sweep dead bundles AND delete loose/chunked
    orphans, never double-counting a swept object."""
    from src.version_engine.derived.object_gc import _delete_eligible_objects

    class FakeBackend:
        def __init__(self):
            self.deleted_individually: list[str] = []

        def sweep_dead_bundles(self, dead):
            # pretend the bundle members got swept whole
            swept = [oid for oid in dead if oid.startswith("bundle")]
            return len(swept), swept

        def delete(self, oid):
            self.deleted_individually.append(oid)
            return True

    backend = FakeBackend()
    repo = SimpleNamespace(store=SimpleNamespace(_backend=backend))
    errors: list[str] = []
    eligible = ["bundle1", "bundle2", "loose1", "chunked1"]

    deleted = _delete_eligible_objects(repo, eligible, errors=errors)

    assert set(deleted) == {"bundle1", "bundle2", "loose1", "chunked1"}
    # swept objects are NOT re-deleted per object
    assert backend.deleted_individually == ["loose1", "chunked1"]
    assert errors == []


def test_version_refs_are_gc_roots(monkeypatch):
    """GAP-3: commits reachable only from stored branch/tag refs must be GC
    roots, or GC reclaims their objects after the retention window."""
    from src.version_engine.derived.object_gc import _add_version_ref_roots
    from src.version_engine.adapters.git.protocol import is_object_id, ZERO_ID
    ref_commit = "f" * 40

    class FakeStore:
        def list_all_commit_ids(self, project_id):
            assert project_id == "p1"
            return [ref_commit, ZERO_ID, "nothex"]  # add() must filter the invalid ones

    roots = set()
    errors = []

    def add(v):  # mirrors collect_object_gc_roots.add
        if isinstance(v, str) and is_object_id(v) and v != ZERO_ID:
            roots.add(v)

    repo = SimpleNamespace(
        history=SimpleNamespace(
            list_version_ref_roots=lambda: FakeStore().list_all_commit_ids("p1")
        )
    )
    _add_version_ref_roots(repo, add, errors)
    assert roots == {ref_commit}
    assert errors == []


def test_version_ref_roots_no_project_id_noop(monkeypatch):
    from src.version_engine.derived.object_gc import _add_version_ref_roots
    roots, errors = set(), []
    _add_version_ref_roots(SimpleNamespace(), roots.add, errors)
    assert roots == set() and errors == []


def test_delete_eligible_without_sweep_method_falls_back():
    """A backend with no sweep_dead_bundles (e.g. FileSystemBackend) still
    deletes per object."""
    from src.version_engine.derived.object_gc import _delete_eligible_objects

    class LooseOnlyBackend:
        def __init__(self):
            self.deleted: list[str] = []

        def delete(self, oid):
            self.deleted.append(oid)
            return True

    backend = LooseOnlyBackend()
    repo = SimpleNamespace(store=SimpleNamespace(_backend=backend))
    errors: list[str] = []
    deleted = _delete_eligible_objects(repo, ["a", "b"], errors=errors)
    assert set(deleted) == {"a", "b"}
    assert backend.deleted == ["a", "b"]
