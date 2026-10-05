"""Tests for PuppyOneServerRepo — scope state, commit history, file ops.

The server repo acts as an adapter glueing the Version Engine repository
interface onto PuppyOne-specific storage: S3 for object bodies, Postgres
(via SupabaseHistoryManager) for commit history and scope state, and an
in-process ScopeManager for scope definitions.

These tests use an in-memory ``FakeHistoryManager`` so we never touch
Supabase — we only care about the interactions between ``ServerRepo``
and the history/audit/scope interfaces.
"""

import threading

import pytest

from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import MODE_DIR, MODE_FILE, TreeEntry, encode_tree


class FakeHistoryManager:
    """In-memory mock for SupabaseHistoryManager (commit_id identity)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._root_hash = ""
        self._head_commit_id = ""
        self._scope_hashes: dict[str, str] = {}
        self._scope_head_commit_ids: dict[str, str] = {}
        self._entries: list[dict] = []
        self._version_index: list[dict] = []
        self._audit = None

    def bind_audit_manager(self, audit) -> None:
        self._audit = audit

    def get_root_hash(self) -> str:
        return self._root_hash

    def initialize_root_hash(self) -> str:
        from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1

        with self._lock:
            if not self._root_hash:
                if self._entries or self._scope_hashes or self._scope_head_commit_ids:
                    raise RuntimeError('legacy_repository_root_corrupt')
                self._root_hash = EMPTY_TREE_SHA1
            return self._root_hash

    def set_root_hash(self, h: str) -> None:
        self._root_hash = h

    def get_head_commit_id(self) -> str:
        return self._head_commit_id

    def set_head_commit_id(self, commit_id: str) -> None:
        self._head_commit_id = commit_id

    def get_scope_hash(self, scope_path: str) -> str:
        return self._scope_hashes.get(scope_path.strip("/"), "")

    def set_scope_hash(self, scope_path: str, h: str) -> None:
        self._scope_hashes[scope_path.strip("/")] = h

    def get_all_scope_hashes(self) -> dict[str, str]:
        return {p: h for p, h in self._scope_hashes.items() if h}

    def get_scope_head_commit_id(self, scope_path: str) -> str:
        with self._lock:
            return self._scope_head_commit_ids.get(scope_path.strip("/"), "")

    def set_scope_head_commit_id(self, scope_path: str, commit_id: str) -> None:
        with self._lock:
            self._scope_head_commit_ids[scope_path.strip("/")] = commit_id

    def record(self, commit_id, who, message, scope_path, changes,
               conflicts=None, root_hash="", scope_hash="", created_at_iso=""):
        self._entries.append({
            "commit_id": commit_id,
            "who": who,
            "message": message,
            "scope_path": scope_path,
            "changes": changes or [],
            "conflicts": conflicts or [],
            "root_hash": root_hash,
            "scope_hash": scope_hash,
            "root": root_hash,
            "created_at": created_at_iso,
        })

    def record_version_index(
        self,
        *,
        scope_path: str,
        source_commit_id: str,
        source_scope_hash: str,
        project_root_hash: str,
        project_view_commit_id: str,
    ) -> None:
        self._version_index = [
            row for row in self._version_index
            if row["source_commit_id"] != source_commit_id
        ]
        self._version_index.append({
            "scope_path": scope_path.strip("/"),
            "source_commit_id": source_commit_id,
            "source_scope_hash": source_scope_hash,
            "project_root_hash": project_root_hash,
            "project_view_commit_id": project_view_commit_id,
        })

    def get_latest_project_view_commit_id(self) -> str:
        if not self._version_index:
            return ""
        return self._version_index[-1]["project_view_commit_id"]

    def get_entry(self, commit_id: str) -> dict | None:
        for e in self._entries:
            if e["commit_id"] == commit_id:
                return e
        return None

    def get_since(self, since_commit_id: str = "", scope_path=None, limit=0):
        entries = list(self._entries)
        if since_commit_id:
            anchor = self.get_entry(since_commit_id)
            if anchor is not None:
                idx = self._entries.index(anchor)
                entries = self._entries[idx + 1:]
        if scope_path:
            entries = [e for e in entries if e.get("scope_path") == scope_path]
        # Contract matches the history backend contract:
        # (1) rows ordered (created_at ASC, commit_id ASC);
        # (2) when limit > 0, return the *newest* limit entries (tail),
        #     not the oldest head — e.g. "latest 50 commits".
        if limit > 0:
            entries = entries[-limit:]
        return entries

    def get_previous_scope_hash(self, scope_path: str, before_commit_id: str) -> str:
        norm = scope_path.strip("/")
        anchor = self.get_entry(before_commit_id)
        if anchor is None:
            # everything predates "nothing" → walk all entries for this scope
            relevant = [e for e in self._entries
                        if e.get("scope_path", "").strip("/") == norm]
            return relevant[-1].get("scope_hash", "") if relevant else ""

        idx = self._entries.index(anchor)
        for earlier in reversed(self._entries[:idx]):
            if earlier.get("scope_path", "").strip("/") == norm:
                h = earlier.get("scope_hash", "")
                if h:
                    return h
        return ""

    def get_scope_state(self, scope_path: str) -> tuple[str, str]:
        norm = scope_path.strip("/")
        with self._lock:
            return (
                self._scope_hashes.get(norm, ""),
                self._scope_head_commit_ids.get(norm, ""),
            )

    def cas_update_scope_hash(
        self,
        scope_path: str,
        old_hash: str,
        new_hash: str,
        head_commit_id: str = "",
    ) -> bool:
        norm = scope_path.strip("/")
        with self._lock:
            current = self._scope_hashes.get(norm, "")
            if current != old_hash:
                return False
            self._scope_hashes[norm] = new_hash
            if head_commit_id:
                self._scope_head_commit_ids[norm] = head_commit_id
            return True

    def cas_update_root_hash(self, old_hash: str, new_hash: str) -> bool:
        with self._lock:
            if self._root_hash != old_hash:
                return False
            self._root_hash = new_hash
            return True

    def publish_project_update(
        self,
        *,
        old_root_hash: str,
        new_root_hash: str,
        commit_id: str,
        who: str,
        message: str,
        changes: list,
        conflicts: list | None,
        created_at_iso: str,
        audit_event_type: str,
        audit_agent_id: str,
        audit_detail: dict,
        source_channel: str = "",
        policy: str = "",
        base_commit_id: str = "",
        client_commit_id: str = "",
        proposed_tree_id: str = "",
        intent_type: str = "operation",
        scope_path: str = "",
        scope_hash: str = "",
        scope_head_commit_id: str = "",
        expected_scope_head_commit_id: str | None = None,
    ) -> tuple[bool, int | None]:
        with self._lock:
            if self._root_hash != old_root_hash:
                return False, None
            norm = scope_path.strip("/")
            if expected_scope_head_commit_id is not None:
                current_scope_head = self._scope_head_commit_ids.get(norm, "")
                if current_scope_head != expected_scope_head_commit_id:
                    return False, None
            accepted_scope_hash = scope_hash or new_root_hash
            self._root_hash = new_root_hash
            self._scope_hashes[""] = new_root_hash
            self._scope_head_commit_ids[""] = commit_id
            self._scope_hashes[norm] = accepted_scope_hash
            self._scope_head_commit_ids[norm] = (
                scope_head_commit_id
                if norm and scope_head_commit_id
                else client_commit_id if norm and client_commit_id else commit_id
            )
            self.record(
                commit_id,
                who,
                message,
                norm,
                changes,
                conflicts,
                root_hash=new_root_hash,
                scope_hash=accepted_scope_hash,
                created_at_iso=created_at_iso,
            )
            self._head_commit_id = commit_id
            if self._audit is not None:
                detail = {
                    **(audit_detail or {}),
                    "source_channel": source_channel,
                    "policy": policy,
                    "base_commit_id": base_commit_id,
                    "client_commit_id": client_commit_id,
                    "proposed_tree_id": proposed_tree_id,
                    "intent_type": intent_type,
                }
                self._audit.record(audit_event_type, audit_agent_id, detail)
            return True, None


class FakeAuditManager:
    def __init__(self):
        self.events = []

    def record(self, event_type, agent_id, detail):
        self.events.append({"type": event_type, "agent": agent_id, "detail": detail})


@pytest.fixture
def memory_store(tmp_path):
    """Real ObjectStore backed by temp filesystem."""
    obj_dir = tmp_path / "objects"
    obj_dir.mkdir()
    return ObjectStore(obj_dir)


@pytest.fixture
def server_repo(memory_store):
    from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo
    from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager

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

    scopes = ScopeManager(FakeScopeBackend())

    return PuppyOneServerRepo(
        project_id="test-proj",
        project_name="Test Project",
        store=memory_store,
        history=history,
        audit=audit,
        scopes=scopes,
    )


class TestScopeState:
    """Scope-hash setters live on ``history``, not the ``ServerRepo`` façade
    (P3-4): exposing a non-CAS setter on the façade invites lost-update
    bugs. These tests reach into ``history`` directly to mimic the boot /
    rollback paths that legitimately need the unchecked setter."""

    def test_get_set_scope_hash(self, server_repo):
        assert server_repo.get_scope_hash("docs") == ""
        server_repo.history.set_scope_hash("docs", "abc123")
        assert server_repo.get_scope_hash("docs") == "abc123"

    def test_get_set_scope_head_commit_id(self, server_repo):
        assert server_repo.get_scope_head_commit_id("docs") == ""
        server_repo.set_scope_head_commit_id("docs", "deadbeef12345678")
        assert server_repo.get_scope_head_commit_id("docs") == "deadbeef12345678"

    def test_scopes_independent(self, server_repo):
        server_repo.history.set_scope_hash("docs", "aaa")
        server_repo.history.set_scope_hash("src", "bbb")
        assert server_repo.get_scope_hash("docs") == "aaa"
        assert server_repo.get_scope_hash("src") == "bbb"

    def test_unchecked_setters_not_on_facade(self, server_repo):
        """Regression guard for P3-4 — keep the non-CAS setters off the façade."""
        assert not hasattr(server_repo, "set_root_hash")
        assert not hasattr(server_repo, "set_scope_hash")


class TestGlobalRootHash:
    def test_default_empty(self, server_repo):
        assert server_repo.get_root_hash() == ""

    def test_set_and_read(self, server_repo):
        server_repo.history.set_root_hash("root-abc")
        assert server_repo.get_root_hash() == "root-abc"

    def test_cas_success(self, server_repo):
        server_repo.history.set_root_hash("old")
        assert server_repo.history.cas_update_root_hash("old", "new") is True
        assert server_repo.get_root_hash() == "new"

    def test_cas_failure_keeps_old(self, server_repo):
        server_repo.history.set_root_hash("old")
        assert server_repo.history.cas_update_root_hash("stale", "new") is False
        assert server_repo.get_root_hash() == "old"


class TestCommitHistory:
    def test_record_and_fetch(self, server_repo):
        server_repo.record_history(
            commit_id="cafebabecafebabe",
            who="alice",
            message="add readme",
            scope_path="docs",
            changes=[],
            scope_hash="tree-hash-1",
        )
        entry = server_repo.get_history_entry("cafebabecafebabe")
        assert entry["commit_id"] == "cafebabecafebabe"
        assert entry["scope_hash"] == "tree-hash-1"
        assert entry["who"] == "alice"

    def test_get_since_empty_returns_all_oldest_first(self, server_repo):
        """Contract: ``get_since`` returns ASC order (oldest first), matching
        the history backend contract. The frontend history
        page is the only consumer that wants DESC and it reverses in-place."""
        server_repo.record_history(
            commit_id="aaaaaaaaaaaaaaaa", who="a", message="1",
            scope_path="docs", changes=[], scope_hash="h1",
        )
        server_repo.record_history(
            commit_id="bbbbbbbbbbbbbbbb", who="b", message="2",
            scope_path="docs", changes=[], scope_hash="h2",
        )
        entries = server_repo.history.get_since("", limit=0)
        assert [e["commit_id"] for e in entries] == [
            "aaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbb",
        ]

    def test_get_since_with_limit_keeps_newest_slice(self, server_repo):
        """When ``limit`` is specified, ``get_since`` must return the newest
        ``limit`` commits (the tail), not the oldest ``limit``. This is the
        bug that broke the admin history page when a project exceeded 50
        commits — callers asking for "latest 50" would silently receive the
        earliest 50 instead."""
        for i in range(5):
            server_repo.record_history(
                commit_id=f"{i:016x}", who="a", message=f"m{i}",
                scope_path="docs", changes=[], scope_hash=f"h{i}",
            )
        entries = server_repo.history.get_since("", limit=3)
        # Latest 3 commits, in ASC (chronological) order.
        assert [e["commit_id"] for e in entries] == [
            f"{2:016x}", f"{3:016x}", f"{4:016x}",
        ]


class TestListScopeFiles:
    def test_empty_scope(self, server_repo):
        scope = {"id": "s1", "path": "/docs/", "exclude": [], "mode": "rw"}
        files = server_repo.list_scope_files(scope)
        assert files == {}

    def test_with_scope_hash(self, server_repo, memory_store):
        """list_scope_files uses scope_hash as a legacy fallback when root is absent."""
        blob_hash = memory_store.put_blob(b"hello world")
        tree_hash = memory_store.put_tree(encode_tree([
            TreeEntry(name="readme.md", mode=MODE_FILE, sha1_hex=blob_hash),
        ]))

        server_repo.history.set_scope_hash("docs", tree_hash)

        scope = {"id": "s1", "path": "/docs/", "exclude": [], "mode": "rw"}
        files = server_repo.list_scope_files(scope)
        assert "readme.md" in files
        assert files["readme.md"] == b"hello world"

    def test_root_scope_reads_canonical_root_tree(self, server_repo, memory_store):
        """Root protocol clone reads the same canonical root as the product UI."""
        root_blob = memory_store.put_blob(b"root")
        child_blob = memory_store.put_blob(b"child")
        root_scope_hash = memory_store.put_tree(encode_tree([
            TreeEntry(name="root.txt", mode=MODE_FILE, sha1_hex=root_blob),
        ]))
        child_scope_hash = memory_store.put_tree(encode_tree([
            TreeEntry(name="child.txt", mode=MODE_FILE, sha1_hex=child_blob),
        ]))
        global_root_hash = memory_store.put_tree(encode_tree([
            TreeEntry(name="root.txt", mode=MODE_FILE, sha1_hex=root_blob),
            TreeEntry(name="folder1", mode=MODE_DIR, sha1_hex=child_scope_hash),
        ]))

        server_repo.history.set_scope_hash("", root_scope_hash)
        server_repo.history.set_scope_hash("folder1", child_scope_hash)
        server_repo.history.set_root_hash(global_root_hash)

        files = server_repo.list_scope_files({
            "id": "root", "path": "", "exclude": [], "mode": "rw",
        })

        assert files == {
            "root.txt": b"root",
            "folder1/child.txt": b"child",
        }


class TestWriteAndBuildScopeTree:
    def test_write_then_build(self, server_repo, memory_store):
        scope = {"id": "s1", "path": "/docs/", "exclude": [], "mode": "rw"}
        server_repo.write_scope_files(scope, {"a.txt": b"content-a"})
        tree_hash = server_repo.build_scope_tree(scope)

        from src.version_engine.write_engine.tree import tree_to_flat
        flat = tree_to_flat(memory_store, tree_hash)
        assert "a.txt" in flat
        content = memory_store.get(flat["a.txt"])
        assert content == b"content-a"


class TestCasUpdateScope:
    """PuppyOneServerRepo.cas_update_scope should piggy-back the new
    head_commit_id onto the same atomic RPC that updates scope_hash."""

    def test_updates_hash_and_head_together(self, server_repo):
        ok = server_repo.cas_update_scope(
            "docs",
            old_hash="",
            new_hash="tree-hash-1",
            head_commit_id="1234567890abcdef",
        )
        assert ok is True
        assert server_repo.get_scope_hash("docs") == "tree-hash-1"
        assert server_repo.get_scope_head_commit_id("docs") == "1234567890abcdef"

    def test_losing_cas_does_not_overwrite_head(self, server_repo):
        server_repo.cas_update_scope(
            "docs", old_hash="", new_hash="winner-hash",
            head_commit_id="winner-commit-id",
        )

        ok = server_repo.cas_update_scope(
            "docs", old_hash="stale", new_hash="loser-hash",
            head_commit_id="loser-commit-id",
        )
        assert ok is False
        assert server_repo.get_scope_hash("docs") == "winner-hash"
        assert server_repo.get_scope_head_commit_id("docs") == "winner-commit-id"
