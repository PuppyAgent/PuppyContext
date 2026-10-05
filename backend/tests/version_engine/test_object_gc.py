"""Git-native object GC regression tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.tree import tree_to_flat
from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager

from src.version_engine.write_engine.git_commit import build_git_commit
from src.version_engine.write_engine.tree_objects import build_tree_from_files
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo
from src.version_engine.derived.object_gc import (
    collect_object_gc_roots,
    mark_reachable_objects,
    run_git_object_gc,
)
from src.version_engine.derived.object_gc_worker import process_object_gc_projects

from tests.version_engine.test_server_repo import FakeAuditManager, FakeHistoryManager


@pytest.fixture
def server_repo(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    history = FakeHistoryManager()
    audit = FakeAuditManager()

    class FakeScopeBackend:
        def __init__(self):
            self._scopes = {}

        def get(self, sid):
            return self._scopes.get(sid)

        def put(self, sid, scope):
            self._scopes[sid] = scope

        def delete(self, sid):
            return self._scopes.pop(sid, None) is not None

        def list_all(self):
            return list(self._scopes.values())

    return PuppyOneServerRepo(
        project_id="test-proj",
        project_name="Test Project",
        store=store,
        history=history,
        audit=audit,
        scopes=ScopeManager(FakeScopeBackend()),
    )


def _commit_tree(repo, tree_id: str, message: str) -> str:
    return build_git_commit(
        repo,
        tree_sha=tree_id,
        parent_sha="",
        who="git:test",
        message=message,
        created_at_iso="2026-01-01T00:00:00+00:00",
    )


def _publish_root(repo, tree_id: str, commit_id: str) -> None:
    repo.history.set_scope_hash("", tree_id)
    repo.history.set_scope_head_commit_id("", commit_id)
    repo.history.set_head_commit_id(commit_id)
    repo.history.set_root_hash(tree_id)
    repo.history.record(
        commit_id,
        "git:test",
        "publish",
        "",
        [{"path": "keep.txt", "action": "add"}],
        scope_hash=tree_id,
        root_hash=tree_id,
    )


def test_git_object_gc_dry_run_reports_orphan_without_deleting(server_repo):
    live_tree = build_tree_from_files(server_repo.store, {"keep.txt": b"keep"})
    live_commit = _commit_tree(server_repo, live_tree, "live")
    _publish_root(server_repo, live_tree, live_commit)

    orphan_tree = build_tree_from_files(server_repo.store, {"drop.txt": b"drop"})
    orphan_commit = _commit_tree(server_repo, orphan_tree, "orphan")

    result = run_git_object_gc(
        server_repo,
        dry_run=True,
        retention_seconds=0,
    )

    assert result.dry_run is True
    assert result.unreachable_count >= 2
    assert orphan_commit in result.unreachable_sample
    assert server_repo.store.exists(orphan_commit)


def test_git_object_gc_deletes_only_unreachable_objects(server_repo):
    live_tree = build_tree_from_files(server_repo.store, {"keep.txt": b"keep"})
    live_commit = _commit_tree(server_repo, live_tree, "live")
    _publish_root(server_repo, live_tree, live_commit)
    live_blob = tree_to_flat(server_repo.store, live_tree)["keep.txt"]

    orphan_tree = build_tree_from_files(server_repo.store, {"drop.txt": b"drop"})
    orphan_blob = tree_to_flat(server_repo.store, orphan_tree)["drop.txt"]
    orphan_commit = _commit_tree(server_repo, orphan_tree, "orphan")

    result = run_git_object_gc(
        server_repo,
        dry_run=False,
        retention_seconds=0,
    )

    assert result.deleted_count >= 3
    assert server_repo.store.exists(live_commit)
    assert server_repo.store.exists(live_tree)
    assert server_repo.store.exists(live_blob)
    assert not server_repo.store.exists(orphan_commit)
    assert not server_repo.store.exists(orphan_tree)
    assert not server_repo.store.exists(orphan_blob)


def test_git_object_gc_requires_durable_quarantine_window_before_delete(server_repo):
    orphan = _put_raw_object(server_repo.store, b"recoverable orphan")
    first_seen: dict[str, datetime] = {}

    def sync_candidates(object_ids, *, now, quarantine_seconds):
        current = set(object_ids)
        for object_id in list(first_seen):
            if object_id not in current:
                first_seen.pop(object_id)
        for object_id in current:
            first_seen.setdefault(object_id, now)
        cutoff = now - timedelta(seconds=quarantine_seconds)
        return [oid for oid in current if first_seen[oid] <= cutoff]

    def remove_candidates(object_ids):
        for object_id in object_ids:
            first_seen.pop(object_id, None)

    server_repo.history.sync_object_gc_candidates = sync_candidates
    server_repo.history.remove_object_gc_candidates = remove_candidates
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    first = run_git_object_gc(
        server_repo,
        dry_run=False,
        retention_seconds=0,
        quarantine_seconds=3600,
        now=started,
    )
    assert first.deleted_count == 0
    assert first.quarantined_count >= 1
    assert server_repo.store.exists(orphan)

    second = run_git_object_gc(
        server_repo,
        dry_run=False,
        retention_seconds=0,
        quarantine_seconds=3600,
        now=started + timedelta(hours=2),
    )
    assert second.deleted_count >= 1
    assert not server_repo.store.exists(orphan)
    assert orphan not in first_seen


def test_git_object_gc_fails_closed_without_quarantine_registry(server_repo):
    orphan = _put_raw_object(server_repo.store, b"orphan")
    result = run_git_object_gc(
        server_repo,
        dry_run=False,
        retention_seconds=0,
        quarantine_seconds=3600,
    )

    assert result.sweep_skipped_for_safety is True
    assert result.deleted_count == 0
    assert server_repo.store.exists(orphan)


def test_git_object_gc_does_not_follow_non_git_raw_tree_children(server_repo):
    raw_blob = _put_raw_object(server_repo.store, b"old raw bytes")
    raw_tree = _put_raw_object(
        server_repo.store,
        json.dumps({"old.txt": ["B", raw_blob]}).encode("utf-8"),
    )
    orphan_raw = _put_raw_object(server_repo.store, b"raw orphan")

    repo = server_repo
    repo.history.set_scope_hash("", raw_tree)
    repo.history.set_root_hash(raw_tree)

    roots = collect_object_gc_roots(repo)
    reachable = mark_reachable_objects(repo, roots)
    assert raw_tree in reachable
    assert raw_blob not in reachable

    result = run_git_object_gc(repo, dry_run=False, retention_seconds=0)

    assert result.deleted_count >= 2
    assert repo.store.exists(raw_tree)
    assert not repo.store.exists(raw_blob)
    assert not repo.store.exists(orphan_raw)


def test_git_object_gc_retention_keeps_unknown_age_orphans(server_repo):
    live_tree = build_tree_from_files(server_repo.store, {"keep.txt": b"keep"})
    live_commit = _commit_tree(server_repo, live_tree, "live")
    _publish_root(server_repo, live_tree, live_commit)

    orphan_tree = build_tree_from_files(server_repo.store, {"drop.txt": b"drop"})
    orphan_commit = _commit_tree(server_repo, orphan_tree, "orphan")

    result = run_git_object_gc(
        server_repo,
        dry_run=False,
        retention_seconds=60,
    )

    assert result.deleted_count == 0
    assert result.kept_unknown_age_count >= 1
    assert server_repo.store.exists(orphan_commit)


def test_git_object_gc_honors_object_age_metadata(server_repo, monkeypatch):
    live_tree = build_tree_from_files(server_repo.store, {"keep.txt": b"keep"})
    live_commit = _commit_tree(server_repo, live_tree, "live")
    _publish_root(server_repo, live_tree, live_commit)
    orphan_tree = build_tree_from_files(server_repo.store, {"drop.txt": b"drop"})
    orphan_commit = _commit_tree(server_repo, orphan_tree, "orphan")
    now = datetime(2026, 1, 8, tzinfo=timezone.utc)

    backend = server_repo.store._backend
    old = now - timedelta(days=10)
    young = now - timedelta(hours=1)

    def fake_metadata():
        return {
            object_id: {"last_modified": old}
            for object_id in server_repo.store.all_hashes()
        } | {orphan_commit: {"last_modified": young}}

    monkeypatch.setattr(backend, "all_hashes_with_metadata", fake_metadata, raising=False)

    result = run_git_object_gc(
        server_repo,
        dry_run=False,
        retention_seconds=7 * 24 * 60 * 60,
        now=now,
    )

    assert result.kept_young_count == 1
    assert result.kept_protected_descendant_count >= 1
    assert server_repo.store.exists(orphan_commit)
    assert server_repo.store.exists(orphan_tree)


def test_git_object_gc_fails_safe_when_reachability_walk_errors(server_repo, monkeypatch):
    """A read failure mid-walk must NOT lead to deletion.

    Reproduces the "Damaged folder" root cause: if a tree object in the live
    closure is transiently unreadable during the mark phase, its children are
    never marked reachable and would be mis-classified as orphans. GC must
    fail safe — refuse to delete anything for the project — rather than sweep
    those still-referenced objects.
    """
    from src.version_engine.write_engine.git_object_format import decode_tree

    store = server_repo.store
    root_tree = build_tree_from_files(
        store, {"docs/guide.md": b"guide", "keep.txt": b"keep"},
    )
    root_commit = _commit_tree(server_repo, root_tree, "live")
    _publish_root(server_repo, root_tree, root_commit)

    docs_subtree = next(
        entry.sha1_hex
        for entry in decode_tree(store.get_object(root_tree)[1])
        if entry.name == "docs"
    )
    guide_blob = tree_to_flat(store, root_tree)["docs/guide.md"]

    # A genuine orphan that WOULD be swept if the closure were trusted.
    orphan_tree = build_tree_from_files(store, {"drop.txt": b"drop"})
    orphan_commit = _commit_tree(server_repo, orphan_tree, "orphan")

    # Simulate a transient read failure on the docs/ subtree during the walk:
    # its child blob then never gets marked reachable.
    real_get_loose = store.get_loose

    def flaky_get_loose(h):
        if h == docs_subtree:
            raise RuntimeError("transient object-store read error")
        return real_get_loose(h)

    monkeypatch.setattr(store, "get_loose", flaky_get_loose)

    result = run_git_object_gc(server_repo, dry_run=False, retention_seconds=0)

    assert result.sweep_skipped_for_safety is True
    assert result.deleted_count == 0
    assert any("closure incomplete" in err for err in result.errors)
    # The live-but-mis-classified blob is NOT swept despite the walk gap …
    assert store.exists(guide_blob)
    # … and the genuine orphan is also kept (safety over reclamation).
    assert store.exists(orphan_commit)


@pytest.mark.parametrize(
    "source",
    [
        "head",
        "root",
        "scopes",
        "scope_head",
        "history",
        "version_index",
        "outbox",
        "conflicts",
        "version_refs",
        "shadow_snapshots",
    ],
)
def test_git_object_gc_fails_closed_for_every_root_source(
    server_repo,
    monkeypatch,
    source,
):
    live_tree = build_tree_from_files(server_repo.store, {"keep.txt": b"keep"})
    live_commit = _commit_tree(server_repo, live_tree, "live")
    _publish_root(server_repo, live_tree, live_commit)
    orphan_tree = build_tree_from_files(server_repo.store, {"drop.txt": b"drop"})
    orphan_commit = _commit_tree(server_repo, orphan_tree, "orphan")

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{source} unavailable")

    history = server_repo.history
    if source == "head":
        monkeypatch.setattr(server_repo, "get_head_commit_id", fail)
    elif source == "root":
        monkeypatch.setattr(server_repo, "get_root_hash", fail)
    elif source == "scopes":
        monkeypatch.setattr(server_repo, "get_all_scope_hashes", fail)
    elif source == "scope_head":
        history.set_scope_hash("docs", live_tree)
        monkeypatch.setattr(server_repo, "get_scope_head_commit_id", fail)
    elif source == "history":
        monkeypatch.setattr(history, "_entries", None)
        monkeypatch.setattr(history, "get_since", fail)
    elif source == "version_index":
        monkeypatch.setattr(history, "_version_index", None)
        monkeypatch.setattr(history, "list_version_index_roots", fail, raising=False)
    elif source == "outbox":
        monkeypatch.setattr(history, "list_pending_outbox_roots", fail, raising=False)
    elif source == "conflicts":
        monkeypatch.setattr(server_repo.audit, "events", None)
        monkeypatch.setattr(history, "list_pending_conflict_roots", fail, raising=False)
    elif source == "version_refs":
        monkeypatch.setattr(history, "list_version_ref_roots", fail, raising=False)
    elif source == "shadow_snapshots":
        monkeypatch.setattr(history, "list_shadow_snapshot_roots", fail, raising=False)

    result = run_git_object_gc(server_repo, dry_run=False, retention_seconds=0)

    assert result.sweep_skipped_for_safety is True
    assert result.eligible_count == 0
    assert result.deleted_count == 0
    assert any(source.split("_")[0] in error for error in result.errors)
    assert server_repo.store.exists(orphan_commit)


def test_git_object_gc_incomplete_dry_run_is_explicitly_non_actionable(
    server_repo,
    monkeypatch,
):
    monkeypatch.setattr(
        server_repo.history,
        "list_version_ref_roots",
        lambda: (_ for _ in ()).throw(RuntimeError("page 2 failed")),
        raising=False,
    )

    result = run_git_object_gc(server_repo, dry_run=True, retention_seconds=0)

    assert result.sweep_skipped_for_safety is True
    assert result.eligible_count == 0
    assert any("untrusted candidate" in error for error in result.errors)


def test_git_object_gc_invalid_persisted_root_is_non_actionable(server_repo, monkeypatch):
    monkeypatch.setattr(
        server_repo.history,
        "list_version_ref_roots",
        lambda: ["not-a-commit-id"],
        raising=False,
    )

    result = run_git_object_gc(server_repo, dry_run=True, retention_seconds=0)

    assert result.sweep_skipped_for_safety is True
    assert result.eligible_count == 0
    assert any("invalid persisted GC root" in error for error in result.errors)


def test_object_gc_worker_can_run_bounded_manual_project_list(server_repo, monkeypatch):
    manager = MagicMock(spec=VersionRepoManager)
    manager.get_gc_repo.return_value = server_repo
    monkeypatch.setattr(
        "src.version_engine.derived.object_gc_worker.settings.VERSION_OBJECT_GC_ENABLED",
        False,
    )

    tree_id = build_tree_from_files(server_repo.store, {"keep.txt": b"keep"})
    commit_id = _commit_tree(server_repo, tree_id, "live")
    _publish_root(server_repo, tree_id, commit_id)

    results = process_object_gc_projects(
        repo_manager=manager,
        client=object(),
        project_ids=["test-proj"],
        dry_run=True,
        retention_seconds=0,
    )

    assert len(results) == 1
    assert results[0].project_id == "test-proj"
    manager.get_gc_repo.assert_called_once_with("test-proj")
    manager.get_server_repo.assert_not_called()


def _put_raw_object(store: ObjectStore, data: bytes) -> str:
    object_id = hashlib.sha1(data).hexdigest()
    store._backend.put(object_id, data)
    return object_id
