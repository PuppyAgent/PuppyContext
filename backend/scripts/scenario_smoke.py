"""Multi-feature scenario smoke runner.

Runs end-to-end scenarios against the in-memory engine + adapter +
projection stack — no Supabase, no S3, but real engine code. Prints
results to stdout so you can SEE the feature combinations exercise:

  $ uv run python scripts/scenario_smoke.py

  ── A. Multi-actor disjoint writes (auto-merge) ──
  ✓ alice / bob / carol all landed; final tree has 4 files
  ...

Use this for:
  * demo: show stakeholders the engine end-to-end
  * sanity-check: after refactoring, run this once and eyeball output
  * documentation: copy/paste the printed flow into a tutorial

For deterministic CI assertions use ``tests/version_engine/test_scenarios_deep.py``
— it covers the same scenarios with explicit asserts.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

# Ensure backend/src on the path when run directly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from src.version_engine.adapters.batch.in_process_client import (
    InProcessVersionClient,
)
from src.version_engine.adapters.git.submission import submit_git_tree
from src.version_engine.adapters.product.operation_adapter import (
    ProductOperationAdapter,
)
from src.version_engine.admission.permission import (
    ensure_repo_writable,
)
from src.version_engine.admission.repo_facade import RepoFacade
from src.version_engine.admission.target import admit_target
from src.version_engine.domain.intents import (
    ConflictResolutionIntent,
    ProjectWriteState,
    VersionSubmissionIntent,
)
from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager
from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo
from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.write_engine.git_commit import build_git_commit
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.tree_objects import (
    build_tree_from_files,
    flatten_tree_to_bytes,
)
from fastapi import HTTPException


PROJECT_ID = "smoke-project"


# ══════════════════════════════════════════════════════════════
# Reusable in-memory harness
# ══════════════════════════════════════════════════════════════


class FakeAuditManager:
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event_type, agent_id, detail):
        self.events.append({"type": event_type, "agent": agent_id, "detail": detail})


class FakeScopeBackend:
    def __init__(self):
        self._scopes: dict[str, dict] = {}

    def get(self, sid):
        return self._scopes.get(sid)

    def put(self, sid, scope):
        self._scopes[sid] = scope

    def delete(self, sid):
        return self._scopes.pop(sid, None) is not None

    def list_all(self):
        return list(self._scopes.values())

    def find_by_path_prefix(self, prefix: str):
        return [s for s in self._scopes.values() if s.get("path", "").startswith(prefix)]


class FakeHistoryManager:
    """Smaller copy of tests/version_engine/test_server_repo.FakeHistoryManager."""

    def __init__(self):
        self._lock = threading.RLock()
        self._root_hash = ""
        self._head_commit_id = ""
        self._scope_hashes: dict[str, str] = {}
        self._scope_head_commit_ids: dict[str, str] = {}
        self._entries: list[dict] = []
        self._version_index: list[dict] = []
        self._audit = None

    def bind_audit_manager(self, audit):
        self._audit = audit

    def get_root_hash(self):
        return self._root_hash

    def set_root_hash(self, h):
        self._root_hash = h

    def get_head_commit_id(self):
        return self._head_commit_id

    def set_head_commit_id(self, commit_id):
        self._head_commit_id = commit_id

    def get_scope_hash(self, scope_path):
        return self._scope_hashes.get(scope_path.strip("/"), "")

    def set_scope_hash(self, scope_path, h):
        self._scope_hashes[scope_path.strip("/")] = h

    def get_all_scope_hashes(self):
        return {p: h for p, h in self._scope_hashes.items() if h}

    def get_scope_head_commit_id(self, scope_path):
        with self._lock:
            return self._scope_head_commit_ids.get(scope_path.strip("/"), "")

    def set_scope_head_commit_id(self, scope_path, commit_id):
        with self._lock:
            self._scope_head_commit_ids[scope_path.strip("/")] = commit_id

    def record(self, commit_id, who, message, scope_path, changes,
               conflicts=None, root_hash="", scope_hash="", created_at_iso=""):
        self._entries.append({
            "commit_id": commit_id, "who": who, "message": message,
            "scope_path": scope_path, "changes": changes or [],
            "conflicts": conflicts or [], "root_hash": root_hash,
            "scope_hash": scope_hash, "root": root_hash,
            "created_at": created_at_iso,
        })

    def record_version_index(self, **kwargs):
        self._version_index.append(kwargs)

    def get_latest_project_view_commit_id(self):
        return self._version_index[-1]["project_view_commit_id"] if self._version_index else ""

    def get_entry(self, commit_id):
        for e in self._entries:
            if e["commit_id"] == commit_id:
                return e
        return None

    def get_since(self, since_commit_id="", scope_path=None, limit=0):
        entries = list(self._entries)
        if since_commit_id:
            anchor = self.get_entry(since_commit_id)
            if anchor is not None:
                idx = self._entries.index(anchor)
                entries = self._entries[idx + 1:]
        if scope_path:
            entries = [e for e in entries if e.get("scope_path") == scope_path]
        if limit > 0:
            entries = entries[-limit:]
        return entries

    def get_previous_scope_hash(self, scope_path, before_commit_id):
        norm = scope_path.strip("/")
        anchor = self.get_entry(before_commit_id)
        if anchor is None:
            relevant = [e for e in self._entries if e.get("scope_path", "").strip("/") == norm]
            return relevant[-1].get("scope_hash", "") if relevant else ""
        idx = self._entries.index(anchor)
        for earlier in reversed(self._entries[:idx]):
            if earlier.get("scope_path", "").strip("/") == norm:
                h = earlier.get("scope_hash", "")
                if h:
                    return h
        return ""

    def get_scope_state(self, scope_path):
        norm = scope_path.strip("/")
        with self._lock:
            return (
                self._scope_hashes.get(norm, ""),
                self._scope_head_commit_ids.get(norm, ""),
            )

    def cas_update_scope_hash(self, scope_path, old_hash, new_hash, head_commit_id=""):
        norm = scope_path.strip("/")
        with self._lock:
            if self._scope_hashes.get(norm, "") != old_hash:
                return False
            self._scope_hashes[norm] = new_hash
            if head_commit_id:
                self._scope_head_commit_ids[norm] = head_commit_id
            return True

    def cas_update_root_hash(self, old_hash, new_hash):
        with self._lock:
            if self._root_hash != old_hash:
                return False
            self._root_hash = new_hash
            return True

    def publish_project_update(self, *, old_root_hash, new_root_hash, commit_id,
                                who, message, changes, conflicts, created_at_iso,
                                audit_event_type, audit_agent_id, audit_detail,
                                source_channel="", policy="", base_commit_id="",
                                client_commit_id="", proposed_tree_id="",
                                intent_type="operation", scope_path="",
                                scope_hash="", scope_head_commit_id=""):
        with self._lock:
            if self._root_hash != old_root_hash:
                return False, None
            norm = scope_path.strip("/")
            accepted_scope_hash = scope_hash or new_root_hash
            self._root_hash = new_root_hash
            self._scope_hashes[""] = new_root_hash
            self._scope_head_commit_ids[""] = commit_id
            self._scope_hashes[norm] = accepted_scope_hash
            self._scope_head_commit_ids[norm] = scope_head_commit_id or commit_id
            self.record(commit_id, who, message, norm, changes, conflicts,
                        root_hash=new_root_hash, scope_hash=accepted_scope_hash,
                        created_at_iso=created_at_iso)
            self._head_commit_id = commit_id
            if self._audit is not None:
                self._audit.record(audit_event_type, audit_agent_id, audit_detail or {})
            return True, None


class FakeConflictTable:
    def __init__(self):
        self._rows = {}
        self.events = []

    def seed(self, *, project_id, pending_conflict_id, **fields):
        row = {"pending_conflict_id": pending_conflict_id, "project_id": project_id,
               "status": "pending", "resolver_actor": "", "resolution_commit_id": "",
               "resolution_detail": {}, **fields}
        self._rows[(project_id, pending_conflict_id)] = row

    def load(self, p, pid):
        return self._rows.get((p, pid))

    def load_pending_conflict(self, p, pid):
        return self.load(p, pid)

    def mark_pending_conflict(self, *, project_id, pending_conflict_id, status, resolver_actor):
        self._rows[(project_id, pending_conflict_id)]["status"] = status
        self._rows[(project_id, pending_conflict_id)]["resolver_actor"] = resolver_actor

    def close_pending_conflict(self, *, project_id, pending_conflict_id, status,
                                 resolver_actor, resolution_commit_id, resolution_detail):
        self._rows[(project_id, pending_conflict_id)]["status"] = status
        self._rows[(project_id, pending_conflict_id)]["resolver_actor"] = resolver_actor
        self._rows[(project_id, pending_conflict_id)]["resolution_commit_id"] = resolution_commit_id
        self._rows[(project_id, pending_conflict_id)]["resolution_detail"] = resolution_detail

    def insert_version_transaction(self, **kwargs):
        self.events.append(("txn", kwargs))
        return len(self.events)

    def record_pending_conflict(self, **kwargs):
        self.events.append(("pending_conflict", kwargs))
        self.seed(
            project_id=kwargs["project_id"],
            pending_conflict_id=kwargs["pending_conflict_id"],
            scope_path=kwargs.get("scope_path", ""),
            base_commit_id=kwargs.get("base_commit_id", ""),
            current_commit_id=kwargs.get("current_commit_id", ""),
            client_commit_id=kwargs.get("client_commit_id", ""),
            proposed_tree_id=kwargs.get("proposed_tree_id", ""),
        )


def _build_harness(tmp_dir: Path):
    obj_dir = tmp_dir / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    store = ObjectStore(obj_dir)

    history = FakeHistoryManager()
    audit = FakeAuditManager()
    scopes = ScopeManager(FakeScopeBackend())
    server_repo = PuppyOneServerRepo(
        project_id=PROJECT_ID, project_name="Smoke",
        store=store, history=history, audit=audit, scopes=scopes,
    )

    repo_manager = MagicMock()
    repo_manager.get_server_repo.return_value = server_repo
    repo_manager.get_repo.return_value = server_repo
    repo_manager.get_project_write_state.return_value = ProjectWriteState(
        project_id=PROJECT_ID, project_name="Smoke", role="owner",
        can_write=True, root_hash="", head_commit_id="",
    )
    return server_repo, repo_manager


def _commit(server_repo, tree_id, *, message="msg", parent=""):
    return build_git_commit(
        server_repo, tree_sha=tree_id, parent_sha=parent,
        who="git:smoke", message=message,
        created_at_iso="2026-05-23T00:00:00Z",
    )


def _scope_files(server_repo, scope_path):
    h = server_repo.get_scope_hash(scope_path)
    return flatten_tree_to_bytes(server_repo.store, h) if h else {}


def _section(title: str) -> None:
    print()
    print(f"── {title} ──")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _info(msg: str) -> None:
    print(f"    · {msg}")


# ══════════════════════════════════════════════════════════════
# Scenarios
# ══════════════════════════════════════════════════════════════


async def scenario_a_multi_actor_auto_merge(tmp_dir: Path):
    _section("A. Multi-actor auto-merge (3 actors, disjoint paths)")
    server_repo, repo_manager = _build_harness(tmp_dir / "scenA")

    # Seed
    seed_tree = build_tree_from_files(server_repo.store, {"README.md": b"v0"})
    await submit_git_tree(
        repo_manager, project_id=PROJECT_ID, scope_path="",
        actor="git:init", base_commit_id="", proposed_tree_id=seed_tree,
        client_commit_id=_commit(server_repo, seed_tree, message="init"),
        message="seed",
    )

    engine = VersionWriteEngine(repo_manager, FakeConflictTable())
    base_head = server_repo.get_scope_head_commit_id("")
    base_files = _scope_files(server_repo, "")

    for actor, path, content in [
        ("user:alice", "alice.md", b"A"),
        ("user:bob", "bob.md", b"B"),
        ("user:carol", "carol.md", b"C"),
    ]:
        new_files = {**base_files, path: content}
        new_tree = build_tree_from_files(server_repo.store, new_files)
        commit = _commit(server_repo, new_tree, message=f"{actor}",
                         parent=server_repo.get_scope_head_commit_id(""))
        result = await engine.submit_version(VersionSubmissionIntent(
            project_id=PROJECT_ID, scope_path="", actor=actor,
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=new_tree, client_commit_id=commit,
            proposed_files=new_files, message=f"{actor} adds {path}",
        ))
        _info(f"{actor} → {result.status} (commit={result.commit_id[:8]})")
        base_files = _scope_files(server_repo, "")

    final = _scope_files(server_repo, "")
    _ok(f"all three landed; final tree files = {sorted(final.keys())}")
    assert {"README.md", "alice.md", "bob.md", "carol.md"} == set(final.keys())


async def scenario_b_lww_conflict(tmp_dir: Path):
    _section("B. LWW conflict on same path (default policy)")
    server_repo, repo_manager = _build_harness(tmp_dir / "scenB")

    seed_tree = build_tree_from_files(server_repo.store, {"plan.md": b"v0"})
    await submit_git_tree(
        repo_manager, project_id=PROJECT_ID, scope_path="",
        actor="git:init", base_commit_id="", proposed_tree_id=seed_tree,
        client_commit_id=_commit(server_repo, seed_tree, message="init"),
        message="seed",
    )

    engine = VersionWriteEngine(repo_manager, FakeConflictTable())
    base_head = server_repo.get_scope_head_commit_id("")

    # Alice writes
    a_tree = build_tree_from_files(server_repo.store, {"plan.md": b"alice"})
    a_commit = _commit(server_repo, a_tree, message="alice", parent=base_head)
    await engine.submit_version(VersionSubmissionIntent(
        project_id=PROJECT_ID, scope_path="", actor="user:alice",
        source_channel="papi", base_commit_id=base_head,
        proposed_tree_id=a_tree, client_commit_id=a_commit,
        proposed_files={"plan.md": b"alice"}, message="alice",
    ))

    # Bob collides against the same base
    b_tree = build_tree_from_files(server_repo.store, {"plan.md": b"bob"})
    b_commit = _commit(server_repo, b_tree, message="bob", parent=base_head)
    b_result = await engine.submit_version(VersionSubmissionIntent(
        project_id=PROJECT_ID, scope_path="", actor="user:bob",
        source_channel="papi", base_commit_id=base_head,
        proposed_tree_id=b_tree, client_commit_id=b_commit,
        proposed_files={"plan.md": b"bob"}, message="bob",
    ))
    _info(f"alice + bob both write plan.md; LWW outcome = {b_result.status}")

    final = _scope_files(server_repo, "")
    _ok(f"LWW: incoming wins → plan.md = {final['plan.md']!r}")
    assert final["plan.md"] == b"bob"


async def scenario_c_manual_review_resolve(tmp_dir: Path):
    _section("C. Manual review → reviewer resolves (accept)")
    server_repo, repo_manager = _build_harness(tmp_dir / "scenC")
    conflict_table = FakeConflictTable()

    seed_tree = build_tree_from_files(server_repo.store, {"plan.md": b"v0"})
    await submit_git_tree(
        repo_manager, project_id=PROJECT_ID, scope_path="",
        actor="git:init", base_commit_id="", proposed_tree_id=seed_tree,
        client_commit_id=_commit(server_repo, seed_tree, message="init"),
        message="seed",
    )

    engine = VersionWriteEngine(repo_manager, conflict_table)
    base_head = server_repo.get_scope_head_commit_id("")

    a_tree = build_tree_from_files(server_repo.store, {"plan.md": b"alice"})
    a_commit = _commit(server_repo, a_tree, message="alice", parent=base_head)
    await engine.submit_version(VersionSubmissionIntent(
        project_id=PROJECT_ID, scope_path="", actor="user:alice",
        source_channel="papi", base_commit_id=base_head,
        proposed_tree_id=a_tree, client_commit_id=a_commit,
        proposed_files={"plan.md": b"alice"}, message="alice",
    ))

    b_tree = build_tree_from_files(server_repo.store, {"plan.md": b"bob"})
    b_commit = _commit(server_repo, b_tree, message="bob", parent=base_head)
    b_result = await engine.submit_version(VersionSubmissionIntent(
        project_id=PROJECT_ID, scope_path="", actor="user:bob",
        source_channel="papi", base_commit_id=base_head,
        proposed_tree_id=b_tree, client_commit_id=b_commit,
        proposed_files={"plan.md": b"bob"}, message="bob",
        policy_override="manual_review",
    ))
    _info(f"bob opts into manual_review → status={b_result.status}")
    _info(f"pending_conflict_id={b_result.pending_conflict_id[:12]}")

    # Reviewer accepts a merged version.
    merged_tree = build_tree_from_files(server_repo.store, {"plan.md": b"merged: alice + bob"})
    resolve_result = await engine.resolve(ConflictResolutionIntent(
        project_id=PROJECT_ID,
        pending_conflict_id=b_result.pending_conflict_id,
        scope_path="", resolver_actor="user:reviewer",
        source_channel="papi", resolution_tree_id=merged_tree,
        resolution_message="agreed merge",
    ))
    _info(f"reviewer resolves → status={resolve_result.status} commit={resolve_result.commit_id[:8]}")

    final = _scope_files(server_repo, "")
    _ok(f"plan.md after resolve = {final['plan.md']!r}")
    assert final["plan.md"] == b"merged: alice + bob"


async def scenario_d_third_party_connector(tmp_dir: Path):
    _section("D. Third-party connector pushes via batch adapter")
    server_repo, repo_manager = _build_harness(tmp_dir / "scenD")

    client = InProcessVersionClient(
        repo_manager, project_id=PROJECT_ID,
        auth_context={
            "agent": "sync:gmail-connector",
            "_scope": {"id": "scope-root", "path": "", "exclude": [], "mode": "rw"},
        },
    )

    # First sync
    client.push(
        modified={
            "inbox/2026-05-23/msg-001.eml": b"Subject: hello\n",
            "inbox/2026-05-23/msg-002.eml": b"Subject: ping\n",
        },
        deleted=[],
        message="gmail sync round 1",
    )
    _info("round 1: 2 messages added")

    # Incremental sync: one new, one deleted
    client.push(
        modified={"inbox/2026-05-23/msg-003.eml": b"Subject: world\n"},
        deleted=["inbox/2026-05-23/msg-001.eml"],
        message="gmail sync round 2",
    )
    _info("round 2: 1 added, 1 deleted")

    files = _scope_files(server_repo, "")
    _ok(f"final state: {sorted(files.keys())}")
    assert "inbox/2026-05-23/msg-001.eml" not in files
    assert "inbox/2026-05-23/msg-002.eml" in files
    assert "inbox/2026-05-23/msg-003.eml" in files


async def scenario_e_product_write_then_read(tmp_dir: Path):
    _section("E. Product UI save → read back through L4")
    server_repo, repo_manager = _build_harness(tmp_dir / "scenE")

    ops = ProductOperationAdapter(repo_manager)
    result = await ops.write_file(
        project_id=PROJECT_ID, path="docs/onboarding.md",
        content=b"# Welcome\n\nStart here.\n",
        who="user:alice", message="initial doc",
    )
    _info(f"write_file → commit={result.commit_id[:8]}")

    content = ops._reader.read_file(PROJECT_ID, "docs/onboarding.md")
    _ok(f"reader returns {len(content)} bytes — first line: {content.splitlines()[0]!r}")
    assert content.startswith(b"# Welcome")


def scenario_f_auth_revoked():
    _section("F. Auth: revoked access key refused")
    from src.version_engine.admission import identity
    from unittest.mock import patch

    with patch.object(identity, "find_scope_by_access_key",
                       return_value={
                           "id": "scope-1", "project_id": PROJECT_ID,
                           "path": "", "exclude": [], "mode": "rw",
                           "access_key_revoked_at": "2026-05-22T00:00:00Z",
                       }):
        auth = identity.PuppyOneAuthenticator(MagicMock())
        result = auth._try_access_key("revoked-key", PROJECT_ID)
    _info(f"_try_access_key('revoked-key') → {result}")
    _ok("revoked key correctly returns None (auth refuses)")
    assert result is None


def scenario_g_channel_pause():
    _section("G. Channel pause: paused connector blocks request")
    from src.version_engine.admission import channel_pause
    channel_pause._channel_pause_cache.clear()

    class PausedConn:
        id = "conn-1"
        status = "paused"

    class FakeRepo:
        def get_by_scope_provider(self, sid, ch):
            return PausedConn()

    import unittest.mock
    with unittest.mock.patch.object(channel_pause, "AccessModelRepository", lambda: FakeRepo()):
        auth = {"_scope": {"id": "scope-1"}}
        try:
            channel_pause.enforce_channel_pause(auth, "cli")
            _info("no raise (unexpected!)")
            raise AssertionError("paused channel should have raised")
        except HTTPException as exc:
            _info(f"enforce_channel_pause raised HTTPException {exc.status_code}: {exc.detail[:60]}...")
            _ok(f"paused channel rejected with HTTP {exc.status_code}")
            assert exc.status_code == 403


def scenario_h_permission_readonly():
    _section("H. Permission: read-only mode rejects write")
    ro_facade = RepoFacade(
        project_id=PROJECT_ID, repo_id="ap-readonly",
        kind="access_point", scope_path="", excludes=(), mode="r",
    )
    try:
        ensure_repo_writable(ro_facade)
        _info("no raise (unexpected!)")
        raise AssertionError("read-only mode should have raised")
    except HTTPException as exc:
        _info(f"ensure_repo_writable on mode=r raised HTTP {exc.status_code}")
        _ok(f"read-only access point correctly refused write (HTTP {exc.status_code})")
        assert exc.status_code == 403

    rw_facade = RepoFacade(
        project_id=PROJECT_ID, repo_id="ap-rw",
        kind="access_point", scope_path="", excludes=(), mode="rw",
    )
    ensure_repo_writable(rw_facade)
    _info("ensure_repo_writable on mode=rw passes")


def scenario_i_target_admission_factory():
    _section("I. TargetAdmission factory bundles L3 checks")
    from src.version_engine.admission import channel_pause
    import unittest.mock

    with unittest.mock.patch.object(channel_pause, "_KNOWN_CHANNELS", frozenset()):
        # rw facade, write action — should succeed
        facade = RepoFacade(
            project_id=PROJECT_ID, repo_id="ap-rw",
            kind="access_point", scope_path="docs", excludes=("secrets/**",),
            mode="rw",
        )
        admission = admit_target(
            {"_scope": {"id": "x"}}, facade,
            action="write", source_channel="papi",
        )
        _info(f"write admission: actions={sorted(admission.allowed_actions)[:5]}...")
        assert admission.allows("write") and admission.allows("read")

        # r facade, write action — should refuse
        ro_facade = RepoFacade(
            project_id=PROJECT_ID, repo_id="ap-ro",
            kind="access_point", scope_path="", excludes=(), mode="r",
        )
        try:
            admit_target({"_scope": {"id": "x"}}, ro_facade,
                          action="write", source_channel="papi")
            raise AssertionError("write on r mode must refuse")
        except HTTPException as exc:
            _info(f"write on mode=r refused with HTTP {exc.status_code}")
    _ok("L3 gate behaves correctly for rw vs r")


def scenario_j_shadow_snapshot_caps():
    _section("J. Shadow snapshot: entry-count cap returns structured 413")
    # The old byte-size cap on the manifest JSON column went away when
    # manifests moved to S3; only the entry-count + per-file caps
    # remain. Both raise HTTPException(413) directly now.
    from fastapi import HTTPException
    from src.version_engine.entrypoints.http.shadow_snapshot import (
        UpsertShadowSnapshotRequest, ShadowSnapshotEntry,
        _enforce_entry_count, _MAX_FILES_PER_SNAPSHOT,
    )

    # Small payload: passes
    small = UpsertShadowSnapshotRequest(
        project_id=PROJECT_ID,
        manifest=[ShadowSnapshotEntry(path="a.txt", blob_hash="a" * 40)],
    )
    _enforce_entry_count(small)
    _info("small manifest accepted")

    # Oversize entry count: structured 413 body.
    big = UpsertShadowSnapshotRequest(
        project_id=PROJECT_ID,
        manifest=[
            ShadowSnapshotEntry(path=f"f{i}.txt", blob_hash="a" * 40)
            for i in range(_MAX_FILES_PER_SNAPSHOT + 1)
        ],
    )
    try:
        _enforce_entry_count(big)
        raise AssertionError("should have caught oversize entry count")
    except HTTPException as exc:
        assert exc.status_code == 413
        detail = exc.detail or {}
        _info(
            f"caught: limit={detail.get('limit')}, "
            f"actual={detail.get('actual')}, cap={detail.get('cap')}"
        )
        _ok("oversize entry count → HTTP 413 with structured detail")


def scenario_k_health_recommendations():
    _section("K. Health endpoint recommended_actions for all four states")
    from unittest.mock import patch
    from src.version_engine.adapters.git import health
    from src.version_engine.adapters.git.view_projection import GitViewHead

    states_seen = {}
    for state in ("empty", "healthy", "history_degraded", "current_corrupt"):
        with patch.object(health, "resolve_git_view_head",
                          return_value=GitViewHead(
                              head="" if state == "empty" else "a" * 40,
                              canonical_head="" if state == "empty" else "a" * 40,
                              health=state,
                              history_cut=(state == "history_degraded"),
                              reason="",
                          )):
            payload = health.git_view_health_payload(
                None, project_id=PROJECT_ID, scope_path="", scope_excludes=[],
            )
        actions = [a["type"] for a in payload["recommended_actions"]]
        states_seen[state] = actions
        _info(f"{state:18s} → git_usable={payload['git_usable']:1} actions={actions}")
    _ok("all four states return at least one recommended_action")
    assert all(a for a in states_seen.values()), "every state must have ≥1 action"


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════


async def main():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="puppyone-smoke-") as tmp:
        tmp_dir = Path(tmp)
        print("=" * 64)
        print("PuppyOne Version Engine — multi-feature scenario smoke")
        print("=" * 64)

        await scenario_a_multi_actor_auto_merge(tmp_dir)
        await scenario_b_lww_conflict(tmp_dir)
        await scenario_c_manual_review_resolve(tmp_dir)
        await scenario_d_third_party_connector(tmp_dir)
        await scenario_e_product_write_then_read(tmp_dir)
        scenario_f_auth_revoked()
        scenario_g_channel_pause()
        scenario_h_permission_readonly()
        scenario_i_target_admission_factory()
        scenario_j_shadow_snapshot_caps()
        scenario_k_health_recommendations()

        print()
        print("=" * 64)
        print("✓ All scenarios completed successfully")
        print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
