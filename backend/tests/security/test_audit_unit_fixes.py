"""Hermetic unit tests for the audit fixes (ISSUE-007/009/011/013 + 002 helpers).

None of these touch Supabase, the network, or any external service — they
exercise pure functions, temp dirs, and in-process token crypto only. Safe to
run against any environment (including one whose .env points at a cloud DB
branch) because no DB client is ever constructed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest

# ── ISSUE-009: shared sandbox command policy ────────────────────────────────

class TestCommandPolicy:
    def _mod(self):
        from src.platform.scope_sandbox import execution_policy as command_policy
        return command_policy

    @pytest.mark.parametrize("cmd", [
        "sudo rm -rf /",
        "cat /etc/passwd",
        "head /proc/self/environ",
        "ls /sys/",
        "cat /dev/mem",
        "curl http://169.254.169.254/latest/meta-data/",
        "mount /dev/sda1 /mnt",
        "reboot",
        "mkfs.ext4 /dev/sdb",
    ])
    def test_forbidden_commands_are_rejected(self, cmd):
        cp = self._mod()
        with pytest.raises(cp.SandboxCommandRejected):
            cp.assert_command_allowed(cmd)

    @pytest.mark.parametrize("cmd", [
        "jq '.' /workspace/data.json",
        "ls -la /workspace",
        "python3 process.py",
        "echo hello",
        "cat /workspace/notes.md",
    ])
    def test_benign_commands_are_allowed(self, cmd):
        cp = self._mod()
        cp.assert_command_allowed(cmd)  # must not raise

    def test_non_string_is_ignored(self):
        cp = self._mod()
        cp.assert_command_allowed(None)  # type: ignore[arg-type]


# ── ISSUE-013: CAS retry backoff ────────────────────────────────────────────

class TestCasBackoff:
    def test_first_attempt_does_not_sleep(self, monkeypatch):
        from src.version_engine.write_engine import cas_backoff as cb

        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(cb.asyncio, "sleep", fake_sleep)
        asyncio.run(cb.cas_backoff(0))
        assert slept == []  # attempt 0 => no delay

    def test_later_attempts_sleep_within_bounds(self, monkeypatch):
        from src.version_engine.write_engine import cas_backoff as cb

        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(cb.asyncio, "sleep", fake_sleep)
        for attempt in range(1, 6):
            asyncio.run(cb.cas_backoff(attempt))

        assert len(slept) == 5
        ceiling_s = cb._CAS_BACKOFF_MAX_MS / 1000.0
        for delay in slept:
            assert 0.0 <= delay <= ceiling_s + 1e-9

    def test_exhausted_cas_maps_to_retryable_http_conflict(self):
        from fastapi import Request

        from src.exception_handler import app_exception_handler
        from src.exceptions import CasRetriesExhausted, ErrorCode

        request = Request({"type": "http", "headers": []})
        response = app_exception_handler(request, CasRetriesExhausted())
        payload = json.loads(response.body)
        assert response.status_code == 409
        assert response.headers["retry-after"] == "1"
        assert payload["code"] == ErrorCode.CAS_RETRY_EXHAUSTED
        assert payload["data"]["retryable"] is True


# ── ISSUE-002: credential masking helpers (pure) ────────────────────────────

class TestAccessListMaskingHelpers:
    def _mod(self):
        from src.platform.access import router
        return router

    def test_redact_config_strips_secrets(self):
        r = self._mod()
        cfg = {
            "mcp_api_key": "secret",
            "api_key": "secret",
            "access_key": "secret",
            "token": "secret",
            "sync_url": "https://example.com",  # non-secret, kept
            "name": "my connector",             # kept
        }
        out = r._redact_config(cfg)
        assert "sync_url" in out and "name" in out
        for secret_key in ("mcp_api_key", "api_key", "access_key", "token"):
            assert secret_key not in out


# ── ISSUE-011: git view cache LRU prune ─────────────────────────────────────

class TestViewCachePrune:
    def _make_view(self, root, project, view_id, *, mtime, size_bytes=1024):
        view_dir = root / project / view_id
        view_dir.mkdir(parents=True, exist_ok=True)
        (view_dir / "view.json").write_text("{}", encoding="utf-8")
        (view_dir / "blob.bin").write_bytes(b"0" * size_bytes)
        # Control recency via view.json mtime.
        os.utime(view_dir / "view.json", (mtime, mtime))
        return view_dir

    def test_prunes_oldest_beyond_max_views(self, tmp_path, monkeypatch):
        from src.config import settings
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        oldest = self._make_view(root, "projA", "v_old", mtime=1_000)
        middle = self._make_view(root, "projA", "v_mid", mtime=2_000)
        newest = self._make_view(root, "projA", "v_new", mtime=3_000)

        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_VIEWS", 2, raising=False)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_BYTES", 0, raising=False)

        view_cache.prune_git_view_cache(keep=newest)

        assert not oldest.exists(), "oldest view should be evicted"
        assert middle.exists()
        assert newest.exists(), "the just-written (keep) view must survive"

    def test_keep_dir_never_evicted_even_if_oldest(self, tmp_path, monkeypatch):
        from src.config import settings
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        keep_old = self._make_view(root, "projA", "v_keep", mtime=1_000)  # oldest
        newer = self._make_view(root, "projA", "v_newer", mtime=5_000)

        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_VIEWS", 1, raising=False)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_BYTES", 0, raising=False)

        view_cache.prune_git_view_cache(keep=keep_old)

        assert keep_old.exists(), "keep dir must not be evicted"
        assert not newer.exists(), "non-keep view should be evicted to meet cap"

    def test_zero_caps_disable_pruning(self, tmp_path, monkeypatch):
        from src.config import settings
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        v1 = self._make_view(root, "projA", "v1", mtime=1_000)
        v2 = self._make_view(root, "projA", "v2", mtime=2_000)

        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_VIEWS", 0, raising=False)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_BYTES", 0, raising=False)

        view_cache.prune_git_view_cache(keep=None)
        assert v1.exists() and v2.exists()

    def test_byte_cap_evicts_until_actual_size_is_bounded(self, tmp_path, monkeypatch):
        from src.config import settings
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        oldest = self._make_view(root, "proj", "old", mtime=1, size_bytes=900)
        newest = self._make_view(root, "proj", "new", mtime=2, size_bytes=900)
        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_VIEWS", 10, raising=False)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_BYTES", 1200, raising=False)

        view_cache.prune_git_view_cache(keep=newest)
        assert not oldest.exists()
        assert newest.exists()

    def test_prune_skips_active_view_and_continues(self, tmp_path, monkeypatch):
        from src.config import settings
        from src.version_engine.adapters.git import view_cache
        from src.version_engine.adapters.git._filelock import file_exclusive_lock

        root = tmp_path / "cache"
        active = self._make_view(root, "proj", "active", mtime=1)
        other = self._make_view(root, "proj", "other", mtime=2)
        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_VIEWS", 1, raising=False)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_BYTES", 0, raising=False)

        with file_exclusive_lock(view_cache.view_lock_path(active)):
            view_cache.prune_git_view_cache()
            assert active.exists()
            assert not other.exists()

    def test_failed_delete_is_not_counted_as_freed(self, tmp_path, monkeypatch):
        from src.config import settings
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        oldest = self._make_view(root, "proj", "old", mtime=1)
        second = self._make_view(root, "proj", "second", mtime=2)
        newest = self._make_view(root, "proj", "new", mtime=3)
        real_rmtree = shutil.rmtree

        def selective_failure(path, *args, **kwargs):
            if path == oldest:
                return None
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        monkeypatch.setattr(view_cache.shutil, "rmtree", selective_failure)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_VIEWS", 1, raising=False)
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_MAX_BYTES", 0, raising=False)
        view_cache.prune_git_view_cache(keep=newest)

        assert oldest.exists()
        assert not second.exists(), "pruner must continue after a failed deletion"
        assert newest.exists()


class TestGitViewCacheInvalidation:
    def _key(self):
        from src.version_engine.adapters.git.view_cache import GitViewCacheKey

        return GitViewCacheKey(
            project_id="project-1",
            object_store="test-store",
            scope_path="",
            scope_excludes=(),
            projection_version="git-view-v1",
            history_mode="full",
            blob_mode="included",
        )

    def test_missing_cache_is_a_valid_first_rebuild_state(self, tmp_path, monkeypatch):
        """A first rebuild must not fail merely because there is no cache yet."""
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)

        view_cache.invalidate_git_view_cache(self._key())

        assert not self._key().cache_dir().exists()

    def test_existing_cache_is_removed(self, tmp_path, monkeypatch):
        from src.version_engine.adapters.git import view_cache

        root = tmp_path / "cache"
        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        cache_dir = self._key().cache_dir()
        cache_dir.mkdir(parents=True)
        (cache_dir / "view.json").write_text("{}", encoding="utf-8")

        view_cache.invalidate_git_view_cache(self._key())

        assert not cache_dir.exists()

    def test_first_transport_rebuild_rewarms_without_a_prior_cache(
        self, tmp_path, monkeypatch
    ):
        """The admin repair endpoint must work before a cache exists."""
        from types import SimpleNamespace

        from src.version_engine.adapters.git import view_cache
        from src.version_engine.derived import git_transport_cache

        root = tmp_path / "cache"
        monkeypatch.setattr(view_cache, "git_view_cache_root", lambda: root)
        calls = []

        def fake_warm(repo, scope_path, scope_excludes, **kwargs):
            calls.append((repo, scope_path, scope_excludes, kwargs))
            return "new-canonical-head"

        monkeypatch.setattr(
            git_transport_cache, "warm_transport_bare_repo", fake_warm
        )
        repo = SimpleNamespace(
            _project_id="project-1",
            store=SimpleNamespace(dir=tmp_path / "object-store"),
        )

        result = git_transport_cache.rebuild_git_transport_view(
            repo,
            scope_path="",
            scope_excludes=[],
            follow_history=True,
            include_blobs=True,
        )

        assert result["head"] == "new-canonical-head"
        assert calls == [(repo, "", [], {"follow_history": True, "include_blobs": True})]
