"""Regression: bundled-object existence check must survive a missing
``mut_object_locations`` row.

Backstory (Railway staging, May 2026): immediately after a ``mv``
op on a fresh project, ``GET /git/{project_id}.git/health`` returned
``current_corrupt`` and Git clones started bouncing with HTTP 409.
The next commit (delete) self-healed it.

The race lives inside ``S3StorageBackend.exists_many``:

  1. Object ``B`` was bundled in an earlier write — its location row
     is in ``mut_object_locations``, its bytes are inside a bundle
     at ``<bundle_key>`` (no per-hash loose key).
  2. A subsequent ``exists_many`` call needs ``B``. The in-memory
     ``_location_cache`` doesn't have ``B`` (different worker, cache
     not propagated, or process restart).
  3. The bulk ``_lookup_many_object_locations`` Supabase query
     *misses* the row for ``B`` — replica lag, connection rotation,
     or any other transient — even though the row exists on the
     primary. The bulk query itself succeeded so the code marked
     ``location_lookup_completed = True``.
  4. The fallback ``async_exists_many`` was then called with
     ``check_packed_locations=False`` — only checks
     ``s3.file_exists(loose_key)``, which returns False because the
     bundle path doesn't materialize loose keys.
  5. ``exists_many`` returns the set without ``B`` → tree validator
     reports the tree invalid → view health = ``current_corrupt``.

The fix forces ``check_packed_locations=True`` on the fallback. The
per-hash ``async_exists`` then retries the location lookup (with both
an early and a late retry inside), catching the row the bulk query
missed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.version_engine.storage.backends.s3 import (
    ObjectLocation,
    S3StorageBackend,
)


class _StubS3Service:
    """Minimal S3 stub — no loose keys for bundled objects."""

    def __init__(self):
        self.loose_keys: set[str] = set()
        self.file_exists_calls: list[str] = []
        self.upload_file_calls: list[tuple[str, int]] = []
        self.bucket_name = "test-bucket"

    async def file_exists(self, key: str) -> bool:
        self.file_exists_calls.append(key)
        return key in self.loose_keys

    async def upload_file(self, key: str, data: bytes, content_type: str = "") -> None:
        self.upload_file_calls.append((key, len(data)))
        # Bundles upload to bundle/ prefix, loose to objects/. We track both.
        self.loose_keys.add(key)


class _StubSupabaseClient:
    """Lookup ``mut_object_locations`` from an in-memory dict — but
    callers can also DROP a hash to simulate a row that "just missed"
    being visible to this connection.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        # If a hash is in ``invisible``, the bulk query pretends it's
        # not there even when ``rows`` contains it. Per-hash retries
        # bypass ``invisible`` to simulate the row becoming visible on
        # the next read.
        self.invisible: set[str] = set()
        self._bulk_queries: list[list[str]] = []
        self._single_queries: list[str] = []

    def from_(self, *_args, **_kwargs):
        # Provide a chained-builder facade. Real supabase-py exposes
        # ``client.table(...).select(...).eq(...).in_(...).execute()``;
        # we replicate just the surface the code touches.
        return _StubChainedBuilder(self)


class _StubChainedBuilder:
    def __init__(self, parent: _StubSupabaseClient):
        self._parent = parent
        self._cols: list[str] = []
        self._eq: dict[str, str] = {}
        self._in_field: str = ""
        self._in_values: list[str] = []

    def select(self, cols: str) -> _StubChainedBuilder:
        self._cols = [c.strip() for c in cols.split(",")]
        return self

    def eq(self, field: str, value) -> _StubChainedBuilder:
        self._eq[field] = value
        return self

    def in_(self, field: str, values: list[str]) -> _StubChainedBuilder:
        self._in_field = field
        self._in_values = list(values)
        return self

    def execute(self):
        rows = []
        for h in self._in_values:
            row = self._parent.rows.get(h)
            if row is None:
                continue
            # Simulate replica lag — bulk query doesn't see the row.
            if h in self._parent.invisible:
                continue
            rows.append({"object_id": h, **row})
        self._parent._bulk_queries.append(list(self._in_values))
        return _StubResp(rows)


class _StubResp:
    def __init__(self, data):
        self.data = data


def _supabase_wrapper(stub_client: _StubSupabaseClient):
    """Wrap _StubSupabaseClient so it looks like SupabaseClient.client.table(...)."""
    wrapper = MagicMock()
    wrapper.client.table.side_effect = lambda *args, **kwargs: stub_client.from_(
        *args,
        **kwargs,
    )
    return wrapper


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_s3():
    return _StubS3Service()


@pytest.fixture
def stub_supabase():
    return _StubSupabaseClient()


@pytest.fixture
def backend(stub_s3, stub_supabase):
    be = S3StorageBackend(
        stub_s3,
        project_id="test-proj",
        supabase=_supabase_wrapper(stub_supabase),
    )
    return be


# ════════════════════════════════════════════════════════════════
# The race
# ════════════════════════════════════════════════════════════════


class TestBundledExistsRace:
    def test_bundled_object_visible_when_bulk_returns_it(
        self,
        backend,
        stub_supabase,
        stub_s3,
    ):
        """Happy path baseline: bundled object's row is visible → exists."""
        h = "a" * 40
        stub_supabase.rows[h] = {
            "pack_key": "version/test-proj/object-bundles/x",
            "offset_bytes": 0,
            "size_bytes": 10,
        }
        # No loose key — only bundled.
        assert backend.exists_many([h]) == {h}

    def test_bundled_object_visible_via_cache(
        self,
        backend,
        stub_supabase,
        stub_s3,
    ):
        """In-memory _location_cache hit short-circuits Supabase entirely."""
        h = "b" * 40
        # Pre-populate the per-process cache as _async_put_bundle would.
        backend._location_cache[h] = ObjectLocation(
            pack_key="version/test-proj/object-bundles/y",
            offset_bytes=0,
            size_bytes=10,
        )
        assert backend.exists_many([h]) == {h}
        # No Supabase query should have fired.
        assert stub_supabase._bulk_queries == [], "in-memory cache should have answered the lookup"

    def test_regression_bundled_object_missed_by_bulk_recovers_via_retry(
        self,
        backend,
        stub_supabase,
        stub_s3,
    ):
        """The actual race repro: bulk Supabase query returns N-k rows
        (misses k bundled objects), per-hash retry catches them.

        BEFORE the fix this returned an incomplete set → tree validator
        marked the tree invalid → view = current_corrupt.

        AFTER the fix the per-hash retry hits async_exists which
        re-runs ``_lookup_object_location``, finds the row, returns
        True. The exists_many response is complete.
        """
        # Three bundled objects, all with rows in the table.
        hashes = ["c" * 40, "d" * 40, "e" * 40]
        for h in hashes:
            stub_supabase.rows[h] = {
                "pack_key": f"version/test-proj/object-bundles/{h[:4]}",
                "offset_bytes": 0,
                "size_bytes": 10,
            }
        # Simulate: bulk query happens to miss `e*40` (replica lag,
        # connection rotation, etc). The row IS there on the primary,
        # so a per-hash retry — which the fix forces — sees it.
        stub_supabase.invisible.add("e" * 40)

        # Tweak `invisible` mid-flight: once the bulk pass is done,
        # the per-hash retry should see the row.

        # Patch: clear `invisible` after the bulk query records itself
        # so the per-hash retry sees the row, mimicking replica catchup.
        original_execute = _StubChainedBuilder.execute

        def patched_execute(self):
            result = original_execute(self)
            # First bulk call → strip the invisibility mask so the
            # subsequent per-hash retries see the rows.
            if len(self._in_values) > 1:
                stub_supabase.invisible.clear()
            return result

        _StubChainedBuilder.execute = patched_execute
        try:
            result = backend.exists_many(hashes)
        finally:
            _StubChainedBuilder.execute = original_execute

        assert result == set(hashes), (
            f"all three bundled hashes should be reported existing; got {result}"
        )

    def test_regression_per_hash_retry_uses_packed_location_lookup(
        self,
        backend,
        stub_supabase,
        stub_s3,
    ):
        """Tighter assertion: the per-hash fallback must retry the
        location lookup, NOT just check the loose S3 key.

        Before the fix the fallback ran ``s3.file_exists(loose_key)``
        for bundled objects — always False, so a Supabase miss became
        a hard ``object missing`` answer. After the fix the fallback
        re-runs ``async_exists`` which calls
        ``_lookup_object_location`` again.
        """
        h = "f" * 40
        stub_supabase.rows[h] = {
            "pack_key": "version/test-proj/object-bundles/z",
            "offset_bytes": 0,
            "size_bytes": 10,
        }
        stub_supabase.invisible.add(h)  # bulk pass misses
        # Patch: become visible on the next read.
        original_execute = _StubChainedBuilder.execute

        def patched_execute(self):
            result = original_execute(self)
            # After ANY query, drop invisibility. The bulk pass returns
            # empty, then per-hash retry succeeds.
            stub_supabase.invisible.clear()
            return result

        _StubChainedBuilder.execute = patched_execute
        try:
            assert backend.exists_many([h]) == {h}
        finally:
            _StubChainedBuilder.execute = original_execute

        # Sanity: stub_s3.file_exists may have been called per the loose
        # check fallback, but the FINAL True answer came from the
        # location-lookup retry (the loose key was never registered).
        assert h not in stub_s3.loose_keys, (
            "test setup invariant: no loose key was registered for bundled object"
        )

    def test_no_phantom_existence_when_row_truly_missing(
        self,
        backend,
        stub_supabase,
    ):
        """Sanity: a truly-missing object stays missing.

        The fix shouldn't cause false-positives — it only widens the
        retry window for transient invisibility. A hash with NO row in
        the table and NO loose key in S3 must still be reported as
        missing.
        """
        ghost = "0" * 40
        # No row, no loose key.
        assert backend.exists_many([ghost]) == set()
