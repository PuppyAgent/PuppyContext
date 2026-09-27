"""GitHub-import conflict gate (git non-fast-forward analogue).

A re-import overwrites the bound scope, so it must refuse (unless force) when the
project received a COMMITTED write from an external/user channel since the last
import. github (own imports) + scope-sync (projection) are not conflicts.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.platform.synchronize.github import importer


# ── pure channel classification ───────────────────────────────────────

def test_has_external_committed_write_ignores_own_and_system():
    f = importer._has_external_committed_write
    assert f([{"source_channel": "github"}]) is False
    assert f([{"source_channel": "scope-sync"}]) is False
    assert f([{"source_channel": "github"}, {"source_channel": "scope-sync"}]) is False
    assert f([]) is False
    # any external/user channel => divergence
    assert f([{"source_channel": "access_cli"}]) is True
    assert f([{"source_channel": "github"}, {"source_channel": "papi"}]) is True
    assert f([{"source_channel": "access_git"}]) is True


# ── _external_writes_since with a fake supabase client ────────────────

class _Query:
    def __init__(self, rows): self._rows = rows
    def select(self, *_a, **_k): return self
    def eq(self, *_a, **_k): return self
    def gt(self, *_a, **_k): return self
    def order(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self
    def execute(self): return SimpleNamespace(data=self._rows)


class _Client:
    def __init__(self, rows): self._rows = rows
    def table(self, _name): return _Query(self._rows)


class _SB:
    def __init__(self, rows): self.client = _Client(rows)


async def test_external_writes_since_true_for_external(monkeypatch):
    monkeypatch.setattr(importer, "SupabaseClient",
                        lambda: _SB([{"source_channel": "access_cli", "status": "committed"}]))
    assert await importer._external_writes_since("proj", "2026-06-20T00:00:00Z") is True


async def test_external_writes_since_false_for_own_only(monkeypatch):
    monkeypatch.setattr(importer, "SupabaseClient",
                        lambda: _SB([{"source_channel": "github"}, {"source_channel": "scope-sync"}]))
    assert await importer._external_writes_since("proj", "2026-06-20T00:00:00Z") is False


async def test_external_writes_since_false_when_none(monkeypatch):
    monkeypatch.setattr(importer, "SupabaseClient", lambda: _SB([]))
    assert await importer._external_writes_since("proj", "2026-06-20T00:00:00Z") is False


async def test_external_writes_since_fails_open(monkeypatch):
    class _Boom:
        @property
        def client(self):
            raise RuntimeError("db down")
    monkeypatch.setattr(importer, "SupabaseClient", lambda: _Boom())
    # A divergence-check error must NOT block the import (GitHub-authoritative).
    assert await importer._external_writes_since("proj", "x") is False
