"""Canonical reads reject retired storage without deleting source bytes.

Historical objects are read only by the operator migration artifacts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.storage.backends.s3 import (
    S3StorageBackend,
)


class _StubS3:
    """In-memory S3 stub keyed by full S3 key.

    Tracks every ``download_file`` and ``delete_file`` call so tests
    can assert (a) what the engine fetched and (b) what got cleaned up.
    """

    def __init__(self) -> None:
        self.bytes_by_key: dict[str, bytes] = {}
        self.download_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.bucket_name = "test-bucket"

    async def download_file(self, key: str) -> bytes:
        self.download_calls.append(key)
        if key not in self.bytes_by_key:
            # Match the production not-found detection (see
            # ``_is_not_found_error``): the message has to contain one
            # of "not found" / "nosuchkey" / "404" / "does not exist"
            # so the engine treats it as a 404 fall-through rather
            # than a fatal error.
            from src.infra.s3.exceptions import S3FileNotFoundError

            raise S3FileNotFoundError(key)
        return self.bytes_by_key[key]

    async def file_exists(self, key: str) -> bool:
        return key in self.bytes_by_key

    async def delete_file(self, key: str) -> None:
        self.delete_calls.append(key)
        self.bytes_by_key.pop(key, None)

    async def upload_file(self, key: str, data: bytes, content_type: str = "") -> None:
        self.bytes_by_key[key] = data

    async def list_files(
        self,
        prefix: str = "",
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ):
        """Minimal single-page list for the integrity scan. Returns
        ``(items, common_prefixes, next_token, is_truncated)`` matching
        ``S3Service.list_files``; each item exposes ``.key``."""
        from types import SimpleNamespace

        items = [SimpleNamespace(key=k) for k in sorted(self.bytes_by_key) if k.startswith(prefix)]
        return items, [], None, False


def _supabase_wrapper():
    """Return a no-op Supabase wrapper. The deferred-loose read path
    doesn't touch ``mut_object_locations``, but the backend constructor
    expects something with the right shape."""
    wrap = MagicMock()
    wrap.client.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = []
    return wrap


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def stub_s3() -> _StubS3:
    return _StubS3()


@pytest.fixture
def project_id() -> str:
    return "test-proj-019"


@pytest.fixture
def backend(stub_s3: _StubS3, project_id: str) -> S3StorageBackend:
    """A backend with deferred-namespace reads ON — that's the read
    path under test."""
    return S3StorageBackend(
        stub_s3,
        project_id=project_id,
        supabase=_supabase_wrapper(),
    )


# ── Helpers ──────────────────────────────────────────────────────


def _stale_deferred_key(project_id: str, hash_hex: str) -> str:
    """The same key shape the production code uses for deferred-namespace
    reads: ``{namespace}/{project_id}/objects/{shard}/{rest}``."""
    return f"{'mut'}/{project_id}/objects/{hash_hex[:2]}/{hash_hex[2:]}"


def _primary_loose_key(backend: S3StorageBackend, hash_hex: str) -> str:
    """The current-namespace loose key the backend would write to."""
    return backend._key_for(hash_hex)


# ── Tests ────────────────────────────────────────────────────────


class TestDeferredLooseSelfHeal:
    """Sync path."""

    def test_stale_deferred_bytes_do_not_propagate_zlib_error(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        """``get(h)`` for a hash whose ONLY presence is stale raw bytes
        in the deferred namespace must NOT raise ``StorageWriteError``
        — that's what surfaces as ``Bulk push failed: invalid git
        loose object``. Instead the engine should report a clean
        ``ObjectNotFoundError`` so the caller knows the blob is
        genuinely missing.
        """
        # Same hash + same kind of bytes the production bug exposed:
        # a JSON tree payload from the pre-Git protocol that happens
        # to live under what the current code reads as a loose-object
        # key.
        h = "520885e2ece1037b" + "0" * 24  # 40-char hex sha1
        stale_bytes = b'{".trash": ["T", "garbage"], "Note.md": ["B", "..."]}'
        stub_s3.bytes_by_key[_stale_deferred_key(project_id, h)] = stale_bytes

        with pytest.raises(ObjectNotFoundError):
            backend.get(h)

    def test_stale_deferred_bytes_are_preserved_not_deleted(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        """Stale bytes MUST be preserved across reads. The earlier
        iteration of this code deleted them on the assumption they
        were garbage; that was a mistake — pre-Git-protocol JSON
        tree records legitimately live in the deferred namespace,
        and ops needs the option to migrate or hand-clean them
        rather than have read-path side effects destroy them.

        Contract: the engine still treats the stale entry as "not
        readable here" (skip + fall through to 404), but the bytes
        remain in S3 for future recovery / inspection.
        """
        h = "5" + "a" * 39
        stale_key = _stale_deferred_key(project_id, h)
        stub_s3.bytes_by_key[stale_key] = b"not zlib"

        with pytest.raises(ObjectNotFoundError):
            backend.get(h)
        # The bytes survive — no auto-delete.
        assert stub_s3.delete_calls == [], (
            f"read path must not delete user data; saw deletes={stub_s3.delete_calls}"
        )
        assert stale_key in stub_s3.bytes_by_key, (
            "stale bytes must remain in S3 for ops / future migration"
        )

    def test_valid_loose_in_primary_namespace_is_preferred(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        """Even if a stale entry exists in the deferred namespace, a
        valid loose object in the PRIMARY namespace must be returned
        without ever touching the stale key (production order:
        primary loose → deferred loose → packed)."""
        import hashlib
        import zlib

        content = b"hello world"
        framed = b"blob " + str(len(content)).encode() + b"\0" + content
        h = hashlib.sha1(framed).hexdigest()
        loose_bytes = zlib.compress(framed)

        # Stale entry sits in deferred namespace, valid bytes in primary.
        stub_s3.bytes_by_key[_stale_deferred_key(project_id, h)] = b"junk"
        stub_s3.bytes_by_key[_primary_loose_key(backend, h)] = loose_bytes

        # Primary read wins; the stale key is never fetched.
        assert backend.get(h) == loose_bytes
        assert _stale_deferred_key(project_id, h) not in stub_s3.download_calls

    def test_valid_retired_bytes_are_not_runtime_readable(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        """The fallback path is still functional for the LEGITIMATE
        case it was designed for — valid loose bytes in the deferred
        namespace (a project pre-migration) should be returned, not
        rejected as stale."""
        import hashlib
        import zlib

        content = b"legacy but valid content"
        framed = b"blob " + str(len(content)).encode() + b"\0" + content
        h = hashlib.sha1(framed).hexdigest()
        loose_bytes = zlib.compress(framed)

        # Only in deferred — no primary copy.
        stub_s3.bytes_by_key[_stale_deferred_key(project_id, h)] = loose_bytes

        with pytest.raises(ObjectNotFoundError):
            backend.get(h)
        # And no delete — these bytes are valid.
        assert stub_s3.delete_calls == []


class TestDeferredLooseSelfHealAsync:
    """Async path mirror — same contract, same regressions."""

    @pytest.mark.asyncio
    async def test_async_stale_deferred_bytes_do_not_propagate_zlib_error(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        h = "520885e2ece1037b" + "1" * 24
        stub_s3.bytes_by_key[_stale_deferred_key(project_id, h)] = b'{"not": "loose"}'

        with pytest.raises(ObjectNotFoundError):
            await backend.async_get(h)

    @pytest.mark.asyncio
    async def test_async_stale_deferred_bytes_preserved(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        """Async mirror — read-path must not delete the bytes."""
        h = "5" + "b" * 39
        stale_key = _stale_deferred_key(project_id, h)
        stub_s3.bytes_by_key[stale_key] = b"not zlib"

        with pytest.raises(ObjectNotFoundError):
            await backend.async_get(h)
        assert stub_s3.delete_calls == [], (
            f"async read path must not delete; saw deletes={stub_s3.delete_calls}"
        )
        assert stale_key in stub_s3.bytes_by_key


# ════════════════════════════════════════════════════════════════
# Primary-namespace verify + hash-on-write (the 520885e2 residual
# fix). Stale bytes squatting on a PRIMARY loose key used to be
# returned to the caller unverified, so bulk push died with
# "invalid git loose object" and a re-upload of the same file
# never self-healed (``_do_put`` skipped because the key existed).
# ════════════════════════════════════════════════════════════════


def _valid_loose(content: bytes) -> tuple[str, bytes]:
    """Return (sha1_hex, zlib-framed loose bytes) for ``content``."""
    import hashlib
    import zlib

    framed = b"blob " + str(len(content)).encode() + b"\0" + content
    return hashlib.sha1(framed).hexdigest(), zlib.compress(framed)


class TestPrimaryNamespaceVerify:
    """Read paths must verify primary-namespace bytes and fall through
    corrupt ones instead of handing zlib garbage to the caller."""

    def test_stale_primary_bytes_fall_through_to_404(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        h = "520885e2ece1037b" + "0" * 24
        # Garbage (not a valid zlib loose object) on the PRIMARY key.
        stub_s3.bytes_by_key[_primary_loose_key(backend, h)] = b"raw json tree, not loose"
        # Nothing in deferred / packed → clean ObjectNotFound, not a
        # zlib explosion.
        with pytest.raises(ObjectNotFoundError):
            backend.get(h)

    def test_corrupt_primary_does_not_fall_back_to_retired_storage(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        h, good = _valid_loose(b"the real content")
        # Corrupt bytes on primary, valid bytes in deferred → caller
        # gets the valid deferred copy.
        stub_s3.bytes_by_key[_primary_loose_key(backend, h)] = b"corrupt"
        stub_s3.bytes_by_key[_stale_deferred_key(project_id, h)] = good
        with pytest.raises(ObjectNotFoundError):
            backend.get(h)

    def test_valid_primary_bytes_returned(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        h, good = _valid_loose(b"healthy object")
        stub_s3.bytes_by_key[_primary_loose_key(backend, h)] = good
        assert backend.get(h) == good

    @pytest.mark.asyncio
    async def test_async_stale_primary_falls_through(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
        project_id: str,
    ) -> None:
        h = "5" + "c" * 39
        stub_s3.bytes_by_key[_primary_loose_key(backend, h)] = b"not loose"
        with pytest.raises(ObjectNotFoundError):
            await backend.async_get(h)


class TestHashOnWrite:
    """``_do_put`` must overwrite stale bytes squatting on a primary
    key instead of trusting the dedup skip — the write-side half of
    the 520885e2 fix that makes re-upload self-heal."""

    @pytest.mark.asyncio
    async def test_put_overwrites_corrupt_resident_bytes(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
    ) -> None:
        h, good = _valid_loose(b"correct bytes")
        key = _primary_loose_key(backend, h)
        # Stale bytes already squatting on the key from a prior failed push.
        stub_s3.bytes_by_key[key] = b"stale garbage"
        await backend.async_put(h, good)
        # The corrupt bytes were overwritten with the correct object.
        assert stub_s3.bytes_by_key[key] == good

    @pytest.mark.asyncio
    async def test_put_skips_when_resident_bytes_already_valid(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
    ) -> None:
        h, good = _valid_loose(b"already here")
        key = _primary_loose_key(backend, h)
        stub_s3.bytes_by_key[key] = good
        # No upload should happen — dedup skip stands for valid bytes.
        before_uploads = list(stub_s3.bytes_by_key.items())
        await backend.async_put(h, good)
        assert list(stub_s3.bytes_by_key.items()) == before_uploads


# ════════════════════════════════════════════════════════════════
# Background integrity scan (runbook §8①) — sweep primary loose
# objects, report corrupt ones, optionally heal (delete) them.
# ════════════════════════════════════════════════════════════════


class TestPrimaryLooseIntegrityScan:
    @pytest.mark.asyncio
    async def test_scan_reports_corrupt_without_healing_by_default(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
    ) -> None:
        good_h, good = _valid_loose(b"healthy")
        bad_h = "5" + "d" * 39
        stub_s3.bytes_by_key[_primary_loose_key(backend, good_h)] = good
        bad_key = _primary_loose_key(backend, bad_h)
        stub_s3.bytes_by_key[bad_key] = b"corrupt squatter"

        result = await backend.async_scan_primary_loose_integrity(heal=False)
        assert result["checked"] == 2
        assert result["corrupt"] == [bad_h]
        assert result["healed"] == 0
        assert bad_key in stub_s3.bytes_by_key
        assert stub_s3.delete_calls == []

    @pytest.mark.asyncio
    async def test_scan_heals_when_enabled(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
    ) -> None:
        bad_h = "5" + "e" * 39
        bad_key = _primary_loose_key(backend, bad_h)
        stub_s3.bytes_by_key[bad_key] = b"corrupt"

        result = await backend.async_scan_primary_loose_integrity(heal=True)
        assert result["corrupt"] == [bad_h]
        assert result["healed"] == 1
        assert bad_key not in stub_s3.bytes_by_key
        assert bad_key in stub_s3.delete_calls

    @pytest.mark.asyncio
    async def test_scan_clean_project_reports_no_corruption(
        self,
        backend: S3StorageBackend,
        stub_s3: _StubS3,
    ) -> None:
        h1, b1 = _valid_loose(b"one")
        h2, b2 = _valid_loose(b"two")
        stub_s3.bytes_by_key[_primary_loose_key(backend, h1)] = b1
        stub_s3.bytes_by_key[_primary_loose_key(backend, h2)] = b2
        result = await backend.async_scan_primary_loose_integrity()
        assert result["checked"] == 2
        assert result["corrupt"] == []
        assert result["healed"] == 0


# ---------------------------------------------------------------------------
# Regression: the background integrity worker (and the /admin/object-integrity
# endpoint) must unwrap decorator backends (e.g. CachedStorageBackend) via the
# ``_inner`` chain to reach the concrete backend that implements the scan.
# Before the fix the wrapper hid the method and every project was reported
# ``supported=False`` (worker) / 500'd (endpoint).
# ---------------------------------------------------------------------------


class _ScanCapableBackend:
    def __init__(self):
        self.called_with = None

    async def async_scan_primary_loose_integrity(self, heal: bool = False):
        self.called_with = heal
        return {"checked": 3, "corrupt": [], "healed": 0, "truncated": False, "supported": True}


class _CacheWrapper:
    """Mimics CachedStorageBackend: holds ``_inner`` but lacks the scan method."""

    def __init__(self, inner):
        self._inner = inner


def test_worker_unwraps_inner_chain_to_find_scan():
    from types import SimpleNamespace

    from src.version_engine.derived.object_integrity_worker import _scan_one_project

    inner = _ScanCapableBackend()
    store = SimpleNamespace(_backend=_CacheWrapper(inner))
    repo = SimpleNamespace(store=store)
    repos = SimpleNamespace(get_gc_repo=lambda pid: repo)

    result = _scan_one_project(repos, "proj-1", heal=True)

    assert result.supported is True
    assert result.checked == 3
    assert inner.called_with is True  # reached the real backend, passed heal through


def test_worker_reports_unsupported_when_no_scan_anywhere():
    from types import SimpleNamespace

    from src.version_engine.derived.object_integrity_worker import _scan_one_project

    # A backend chain that never exposes the scan method (e.g. filesystem dev).
    store = SimpleNamespace(_backend=SimpleNamespace())
    repo = SimpleNamespace(store=store)
    repos = SimpleNamespace(get_gc_repo=lambda pid: repo)

    result = _scan_one_project(repos, "proj-1", heal=False)
    assert result.supported is False
