"""Deep multi-actor integration scenarios.

Unit tests verify ONE thing in isolation. These tests assemble multiple
actors, multiple scopes, conflicting writes, and the full L1→L5→L6 path
to exercise feature combinations the unit tests cannot.

The harness uses the in-memory ``server_repo`` from
``test_server_repo`` so Supabase is never touched, but the engine,
adapter, conflict policy, merge, projection, and resolver code is
genuinely exercised. Where a scenario needs the L4 ProductOperationAdapter
(typed product writes), we construct it against the same fake
repo_manager so the read/write paths share state.

Coverage:
  A. Multi-actor auto-merge (disjoint paths)
  B. Multi-actor LWW conflict (same path)
  C. Multi-actor manual_review → resolve(accept) → commit
  D. Multi-actor manual_review → resolve(reject) → unchanged
  E. CAS-retry merge: concurrent same-scope writers, both lands
  F. Project-root CAS-retry merge (our new fix from batch 1)
  G. Cross-scope writes: parent + child, root-first graft
  H. Auth: revoked key, channel pause, JWT
  I. Permission: read-only mode rejects writes
  J. Shadow snapshot caps (413 with limit-name body)
  K. Policy override: VersionSubmissionIntent.policy_override = manual_review
  L. Rollback intent carries policy_override
  M. initialize_project_tree idempotent on existing root
  N. Health endpoint states + recommended_actions for all four
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ResolvedRepositoryView, ScopeTarget
from src.repo.models import RepositoryScope, ResolvedScopeCredential
from src.version_engine.adapters.git.submission import submit_git_tree
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.admission.permission import (
    ensure_mode_writable,
    ensure_repo_readable,
    ensure_repo_writable,
)
from src.version_engine.admission.repo_facade import (
    RepoFacade,
    repo_facade_from_auth,
)
from src.version_engine.admission.target import admit_target
from src.version_engine.domain.intents import (
    ConflictResolutionIntent,
    OperationWriteIntent,
    RollbackIntent,
    VersionSubmissionIntent,
)
from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.write_engine.git_commit import build_git_commit
from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1
from src.version_engine.write_engine.tree_objects import (
    build_tree_from_files,
    flatten_tree_to_bytes,
)


def _scope_auth(
    *, project_id: str = "p", scope_id: str = "x", path: str = "scope"
) -> dict:
    target = ScopeTarget(project_id=project_id, scope_id=scope_id)
    return {
        "_runtime_grant": RuntimeGrant(
            principal=RuntimePrincipal(
                principal_id="test-credential",
                credential_kind="test",
            ),
            target=target,
            repository_view=ResolvedRepositoryView(
                target=target,
                path_prefix=path,
                excludes=(),
                max_mode="rw",
            ),
            mode=RuntimeMode.READ_WRITE,
        )
    }


# ════════════════════════════════════════════════════════════════
# Shared harness
# ════════════════════════════════════════════════════════════════


class _FakeConflictTable:
    """Combined ledger + conflict-table stand-in.

    Same shape as ``test_engine_resolve._FakeConflictTable`` but adds
    ``record_pending_conflict`` capture so multi-actor manual_review
    scenarios can inspect what landed in the conflict table.
    """

    def __init__(self):
        self._rows: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self.txns: list[dict] = []
        self.pending_recordings: list[dict] = []

    def seed(self, *, project_id: str, pending_conflict_id: str, **fields) -> None:
        row = {
            "pending_conflict_id": pending_conflict_id,
            "project_id": project_id,
            "status": "pending",
            "scope_path": fields.get("scope_path", ""),
            "current_commit_id": fields.get("current_commit_id", ""),
            "base_commit_id": fields.get("base_commit_id", ""),
            "client_commit_id": fields.get("client_commit_id", ""),
            "proposed_tree_id": fields.get("proposed_tree_id", ""),
            "resolver_actor": "",
            "resolution_commit_id": "",
            "resolution_detail": {},
            "conflict_records": fields.get("conflict_records", []),
        }
        with self._lock:
            self._rows[(project_id, pending_conflict_id)] = row

    def load(self, project_id: str, pending_conflict_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get((project_id, pending_conflict_id))
            return dict(row) if row else None

    def load_pending_conflict(self, project_id: str, pending_conflict_id: str) -> dict | None:
        return self.load(project_id, pending_conflict_id)

    def mark_pending_conflict(self, *, project_id, pending_conflict_id, status, resolver_actor):
        with self._lock:
            row = self._rows[(project_id, pending_conflict_id)]
            row["status"] = status
            row["resolver_actor"] = resolver_actor

    def close_pending_conflict(self, *, project_id, pending_conflict_id, status, resolver_actor,
                                resolution_commit_id, resolution_detail):
        with self._lock:
            row = self._rows[(project_id, pending_conflict_id)]
            row["status"] = status
            row["resolver_actor"] = resolver_actor
            row["resolution_commit_id"] = resolution_commit_id
            row["resolution_detail"] = resolution_detail

    def insert_version_transaction(self, **kwargs):
        self.txns.append(kwargs)
        return len(self.txns)

    def record_pending_conflict(self, **kwargs):
        self.pending_recordings.append(kwargs)
        # Auto-seed the conflict table on first record so resolve() can find it.
        self.seed(
            project_id=kwargs["project_id"],
            pending_conflict_id=kwargs["pending_conflict_id"],
            scope_path=kwargs.get("scope_path", ""),
            base_commit_id=kwargs.get("base_commit_id", ""),
            current_commit_id=kwargs.get("current_commit_id", ""),
            client_commit_id=kwargs.get("client_commit_id", ""),
            proposed_tree_id=kwargs.get("proposed_tree_id", ""),
            conflict_records=kwargs.get("conflict_records", []),
        )


# ════════════════════════════════════════════════════════════════
# Helpers used by multiple scenarios
# ════════════════════════════════════════════════════════════════


def _make_commit(server_repo, tree_id: str, *, message: str = "msg", parent: str = "") -> str:
    return build_git_commit(
        server_repo,
        tree_sha=tree_id,
        parent_sha=parent,
        who="git:test",
        message=message,
        created_at_iso="2026-05-16T00:00:00Z",
    )


def _register_scope(server_repo, scope_path: str) -> None:
    """Register a non-root scope so engine.validate_scope_bound_files recognises it.

    The in-memory ScopeBackend starts empty; without this, the engine
    treats any submission to a non-root scope as cross-scope and rejects
    with ``CrossScopeSubmissionError``.
    """
    if scope_path:
        server_repo.scopes.add(
            scope_id=f"scope-{scope_path}",
            path=scope_path,
            exclude=[],
        )


async def _seed_scope(server_repo, repo_manager, scope_path: str, files: dict[str, bytes]):
    """Publish an initial commit at ``scope_path`` with ``files`` content."""
    _register_scope(server_repo, scope_path)
    tree_id = build_tree_from_files(server_repo.store, files)
    return await submit_git_tree(
        repo_manager,
        project_id="test-proj",
        scope_path=scope_path,
        actor="git:init",
        base_commit_id="",
        proposed_tree_id=tree_id,
        client_commit_id=_make_commit(server_repo, tree_id, message=f"seed {scope_path or 'root'}"),
        message=f"seed {scope_path or 'root'}",
    )


def _scope_files(server_repo, scope_path: str) -> dict[str, bytes]:
    scope_hash = server_repo.get_scope_hash(scope_path)
    if not scope_hash:
        return {}
    return flatten_tree_to_bytes(server_repo.store, scope_hash)


# ════════════════════════════════════════════════════════════════
# A. Multi-actor auto-merge (disjoint paths)
# ════════════════════════════════════════════════════════════════


class TestMultiActorAutoMerge:
    """Three actors writing to disjoint paths must all converge."""

    @pytest.mark.asyncio
    async def test_three_actors_disjoint_paths_all_land(
        self, repo_manager, server_repo,
    ):
        # Seed root with one shared file.
        await _seed_scope(server_repo, repo_manager, "", {"README.md": b"v0"})

        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())

        # Each actor proposes a new tree built on the seed base — same base
        # commit, three disjoint files. With CAS this would normally retry;
        # the merge-on-cas-retry path handles the conflict resolution.
        base_head = server_repo.get_scope_head_commit_id("")
        base_files = _scope_files(server_repo, "")

        for actor, path, content in [
            ("user:alice", "alice.md", b"alice content"),
            ("user:bob", "bob.md", b"bob content"),
            ("user:carol", "carol.md", b"carol content"),
        ]:
            new_files = {**base_files, path: content}
            new_tree = build_tree_from_files(server_repo.store, new_files)
            client_commit = _make_commit(
                server_repo, new_tree, message=f"{actor} adds {path}",
                parent=server_repo.get_scope_head_commit_id(""),
            )
            result = await engine.submit_version(VersionSubmissionIntent(
                project_id="test-proj",
                scope_path="",
                actor=actor,
                source_channel="papi",
                base_commit_id=base_head,
                proposed_tree_id=new_tree,
                client_commit_id=client_commit,
                proposed_files=new_files,
                message=f"{actor} adds {path}",
            ))
            assert result.status == "ok", f"{actor} submission rejected: {result}"

            # Re-fetch base_files so successive writers build on the most
            # recent state (mirrors how real actors would proceed).
            base_files = _scope_files(server_repo, "")

        final = _scope_files(server_repo, "")
        assert b"alice content" == final.get("alice.md")
        assert b"bob content" == final.get("bob.md")
        assert b"carol content" == final.get("carol.md")
        assert b"v0" == final.get("README.md"), "seed file must survive"


# ════════════════════════════════════════════════════════════════
# B. Multi-actor LWW conflict (same path)
# ════════════════════════════════════════════════════════════════


class TestMultiActorLWWConflict:
    """Two actors writing the same path with default policy fall to LWW."""

    @pytest.mark.asyncio
    async def test_same_path_default_lww_keeps_last(
        self, repo_manager, server_repo,
    ):
        await _seed_scope(server_repo, repo_manager, "", {"shared.txt": b"v0"})

        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())
        base_head = server_repo.get_scope_head_commit_id("")

        # Alice writes first
        alice_tree = build_tree_from_files(server_repo.store, {"shared.txt": b"alice"})
        alice_commit = _make_commit(server_repo, alice_tree, message="alice", parent=base_head)
        alice_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:alice",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=alice_tree, client_commit_id=alice_commit,
            proposed_files={"shared.txt": b"alice"}, message="alice",
        ))
        assert alice_result.status == "ok"

        # Bob writes against the SAME base — CAS will lose, merge-on-retry
        # will produce an LWW conflict because both touch shared.txt.
        bob_tree = build_tree_from_files(server_repo.store, {"shared.txt": b"bob"})
        bob_commit = _make_commit(server_repo, bob_tree, message="bob", parent=base_head)
        bob_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:bob",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=bob_tree, client_commit_id=bob_commit,
            proposed_files={"shared.txt": b"bob"}, message="bob",
        ))
        # Default policy is LWW → bob's write wins (incoming wins).
        assert bob_result.status == "ok"
        final = _scope_files(server_repo, "")
        assert final["shared.txt"] == b"bob", (
            "LWW: incoming (bob) should win over server's (alice's) content"
        )


# ════════════════════════════════════════════════════════════════
# C+D. manual_review → resolve(accept) / resolve(reject)
# ════════════════════════════════════════════════════════════════


class TestManualReviewResolveFlow:
    @pytest.mark.asyncio
    async def test_manual_review_then_accept_resolution_lands(
        self, repo_manager, server_repo,
    ):
        await _seed_scope(server_repo, repo_manager, "", {"plan.md": b"v0"})
        conflict_table = _FakeConflictTable()
        engine = VersionWriteEngine(repo_manager, conflict_table)

        base_head = server_repo.get_scope_head_commit_id("")

        # First writer lands cleanly.
        a_tree = build_tree_from_files(server_repo.store, {"plan.md": b"alice plan"})
        a_commit = _make_commit(server_repo, a_tree, message="alice", parent=base_head)
        await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:alice",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=a_tree, client_commit_id=a_commit,
            proposed_files={"plan.md": b"alice plan"}, message="alice",
        ))

        # Second writer opts INTO manual_review and lands against stale base.
        b_tree = build_tree_from_files(server_repo.store, {"plan.md": b"bob plan"})
        b_commit = _make_commit(server_repo, b_tree, message="bob", parent=base_head)
        b_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:bob",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=b_tree, client_commit_id=b_commit,
            proposed_files={"plan.md": b"bob plan"}, message="bob",
            policy_override="manual_review",
        ))
        assert b_result.status == "pending", b_result
        assert b_result.pending_conflict_id

        # Reviewer resolves with a merged version.
        resolved_tree = build_tree_from_files(
            server_repo.store, {"plan.md": b"alice + bob merged"},
        )
        resolve_result = await engine.resolve(ConflictResolutionIntent(
            project_id="test-proj",
            pending_conflict_id=b_result.pending_conflict_id,
            scope_path="",
            resolver_actor="user:reviewer",
            source_channel="papi",
            resolution_tree_id=resolved_tree,
            resolution_message="merged manually",
        ))
        assert resolve_result.status == "ok", resolve_result
        assert _scope_files(server_repo, "")["plan.md"] == b"alice + bob merged"

        row = conflict_table.load("test-proj", b_result.pending_conflict_id)
        assert row["status"] == "resolved"
        assert row["resolver_actor"] == "user:reviewer"

    @pytest.mark.asyncio
    async def test_manual_review_then_reject_leaves_head(
        self, repo_manager, server_repo,
    ):
        await _seed_scope(server_repo, repo_manager, "", {"plan.md": b"v0"})
        conflict_table = _FakeConflictTable()
        engine = VersionWriteEngine(repo_manager, conflict_table)
        base_head = server_repo.get_scope_head_commit_id("")

        # Two actors race; second opts into manual_review.
        a_tree = build_tree_from_files(server_repo.store, {"plan.md": b"alice"})
        a_commit = _make_commit(server_repo, a_tree, message="alice", parent=base_head)
        await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:alice",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=a_tree, client_commit_id=a_commit,
            proposed_files={"plan.md": b"alice"}, message="alice",
        ))
        alice_head = server_repo.get_scope_head_commit_id("")

        b_tree = build_tree_from_files(server_repo.store, {"plan.md": b"bob"})
        b_commit = _make_commit(server_repo, b_tree, message="bob", parent=base_head)
        b_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:bob",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=b_tree, client_commit_id=b_commit,
            proposed_files={"plan.md": b"bob"}, message="bob",
            policy_override="manual_review",
        ))
        assert b_result.status == "pending"

        # Reviewer REJECTS — head must not move.
        reject_result = await engine.resolve(ConflictResolutionIntent(
            project_id="test-proj",
            pending_conflict_id=b_result.pending_conflict_id,
            scope_path="", resolver_actor="user:reviewer",
            source_channel="papi", decision="reject",
            resolution_message="superseded",
        ))
        assert reject_result.status == "rejected"
        assert server_repo.get_scope_head_commit_id("") == alice_head, (
            "reject must not advance the scope head"
        )
        # Alice's version persists.
        assert _scope_files(server_repo, "")["plan.md"] == b"alice"


# ════════════════════════════════════════════════════════════════
# E. CAS-retry merge on scope path (concurrent disjoint writes)
# ════════════════════════════════════════════════════════════════


class TestScopeCASRetryMerge:
    @pytest.mark.asyncio
    async def test_concurrent_disjoint_writers_both_visible(
        self, repo_manager, server_repo,
    ):
        # Both writers against the SAME base; first lands directly, second
        # uses CAS-retry merge to combine their work.
        await _seed_scope(server_repo, repo_manager, "docs", {"index.md": b"v0"})

        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())
        base_head = server_repo.get_scope_head_commit_id("docs")

        # Alice writes alpha.md
        a_tree = build_tree_from_files(
            server_repo.store, {"index.md": b"v0", "alpha.md": b"A"},
        )
        a_commit = _make_commit(server_repo, a_tree, message="alpha", parent=base_head)
        a_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="docs", actor="user:alice",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=a_tree, client_commit_id=a_commit,
            proposed_files={"index.md": b"v0", "alpha.md": b"A"},
            message="alpha",
        ))
        assert a_result.status == "ok"

        # Bob STILL holds base_head as his base — concurrent write.
        b_tree = build_tree_from_files(
            server_repo.store, {"index.md": b"v0", "beta.md": b"B"},
        )
        b_commit = _make_commit(server_repo, b_tree, message="beta", parent=base_head)
        b_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="docs", actor="user:bob",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=b_tree, client_commit_id=b_commit,
            proposed_files={"index.md": b"v0", "beta.md": b"B"},
            message="beta",
        ))
        assert b_result.status == "ok"

        final = _scope_files(server_repo, "docs")
        # Both alpha.md and beta.md must be visible — that's the merge.
        assert final.get("alpha.md") == b"A"
        assert final.get("beta.md") == b"B"
        assert final.get("index.md") == b"v0"


# ════════════════════════════════════════════════════════════════
# F. Project-root CAS-retry merge (our new fix from batch 1)
# ════════════════════════════════════════════════════════════════


class TestProjectRootCASRetryMerge:
    @pytest.mark.asyncio
    async def test_concurrent_root_writers_disjoint_paths(
        self, repo_manager, server_repo,
    ):
        """Same shape as the scope test, but at the project root.

        This exercises ``_apply_project_operation_optimistic`` which used
        to be a blind splice-retry — concurrent root writes would silently
        overwrite. The fix added _merge_on_cas_retry to the root path.
        """
        # Seed root via a Git submission first.
        await _seed_scope(server_repo, repo_manager, "", {"README.md": b"v0"})

        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())
        base_head = server_repo.get_scope_head_commit_id("")

        # Two writers against the same base, disjoint files.
        a_files = {"README.md": b"v0", "alpha.md": b"A"}
        a_tree = build_tree_from_files(server_repo.store, a_files)
        a_commit = _make_commit(server_repo, a_tree, message="alpha", parent=base_head)
        a_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:alice",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=a_tree, client_commit_id=a_commit,
            proposed_files=a_files, message="alpha",
        ))
        assert a_result.status == "ok"

        b_files = {"README.md": b"v0", "beta.md": b"B"}
        b_tree = build_tree_from_files(server_repo.store, b_files)
        b_commit = _make_commit(server_repo, b_tree, message="beta", parent=base_head)
        b_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:bob",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=b_tree, client_commit_id=b_commit,
            proposed_files=b_files, message="beta",
        ))
        assert b_result.status == "ok"

        final = _scope_files(server_repo, "")
        assert final.get("alpha.md") == b"A", "alpha lost — root merge fallback failed"
        assert final.get("beta.md") == b"B", "beta lost — root merge fallback failed"


# ════════════════════════════════════════════════════════════════
# G. Cross-scope writes — verify root-first scope views refresh
# ════════════════════════════════════════════════════════════════


class TestCrossScopeWrites:
    @pytest.mark.asyncio
    async def test_writes_to_child_scope_graft_into_root(
        self, repo_manager, server_repo,
    ):
        """A child-scope write grafts into the canonical root.

        No synthetic scope-promote commit is created; the root view derives
        directly from the accepted project root.
        """
        await _seed_scope(server_repo, repo_manager, "", {"root.md": b"R"})
        await _seed_scope(server_repo, repo_manager, "docs", {"doc.md": b"D"})

        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())

        root_head_before = server_repo.get_scope_head_commit_id("")
        docs_head_before = server_repo.get_scope_head_commit_id("docs")
        assert root_head_before and docs_head_before
        assert root_head_before != docs_head_before

        # Write into docs scope.
        new_docs = {"doc.md": b"D", "new.md": b"docs-only"}
        new_docs_tree = build_tree_from_files(server_repo.store, new_docs)
        new_docs_commit = _make_commit(
            server_repo, new_docs_tree, message="docs", parent=docs_head_before,
        )
        result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="docs", actor="user:alice",
            source_channel="papi", base_commit_id=docs_head_before,
            proposed_tree_id=new_docs_tree, client_commit_id=new_docs_commit,
            proposed_files=new_docs, message="docs",
        ))
        assert result.status == "ok"

        # docs scope advanced.
        assert server_repo.get_scope_head_commit_id("docs") != docs_head_before
        # Root tree contains the docs subtree content via root-first graft.
        root_files = _scope_files(server_repo, "")
        assert root_files.get("docs/new.md") == b"docs-only", (
            f"root projection missing docs/new.md; root_files={list(root_files)}"
        )
        # The original root file is still there.
        assert root_files.get("root.md") == b"R"


# ════════════════════════════════════════════════════════════════
# H. Auth: revoked key, channel pause
# ════════════════════════════════════════════════════════════════


class TestAuth:
    """Direct admission/identity layer tests — verify the L2 gates."""

    def test_revoked_access_key_is_rejected_by_lookup(self, monkeypatch):
        """Revoked credentials are absent from the canonical active lookup."""
        from unittest.mock import MagicMock

        from src.version_engine.admission import identity

        monkeypatch.setattr(
            identity,
            "resolve_scope_access_credential",
            lambda _supa, _key: None,
        )
        auth = identity.PuppyOneAuthenticator(MagicMock())
        result = auth._try_access_key("revoked-key", "test-proj")
        assert result is None, "revoked key must not authenticate"

    def test_access_key_project_mismatch_refused(self, monkeypatch):
        from unittest.mock import MagicMock

        from src.version_engine.admission import identity

        now = datetime.now(timezone.utc)
        resolved = ResolvedScopeCredential(
            credential_id="credential-1",
            credential_type="bearer_token",
            access_surface_id="surface-1",
            scope=RepositoryScope(
                id="scope-1",
                project_id="OTHER-PROJECT",
                name="Docs",
                path="docs",
                exclude=[],
                max_mode="rw",
                created_at=now,
                updated_at=now,
            ),
        )
        monkeypatch.setattr(
            identity,
            "resolve_scope_access_credential",
            lambda _supa, _key: resolved,
        )
        auth = identity.PuppyOneAuthenticator(MagicMock())
        result = auth._try_access_key("any-key", "test-proj")
        assert result is None, "project mismatch must not authenticate"

    def test_access_key_storage_failure_is_retryable(self, monkeypatch):
        from unittest.mock import MagicMock

        from src.version_engine.admission import identity

        auth = identity.PuppyOneAuthenticator(MagicMock())
        monkeypatch.setattr(identity.settings, "SKIP_AUTH", False)
        monkeypatch.setattr(auth, "_try_jwt", lambda _token: None)

        def _unavailable(*_args, **_kwargs):
            raise RuntimeError("storage down")

        monkeypatch.setattr(
            identity,
            "resolve_scope_access_credential",
            _unavailable,
        )

        with pytest.raises(HTTPException) as error:
            auth.authenticate("token", "test-proj")

        assert error.value.status_code == 503
        assert error.value.headers == {"X-PuppyOne-Error-Code": "1010"}

    def test_channel_pause_blocks_paused_connector(self, monkeypatch):
        """AccessSurface status='paused' → HTTPException 403."""
        from src.version_engine.admission import channel_pause

        # Reset the global cache between tests
        channel_pause._channel_pause_cache.clear()

        # Stub AccessModelRepository to return a paused connector.
        class StubConn:
            id = "connector-1"
            status = "paused"

        class StubRepo:
            def get_by_target_kind(self, project_id, scope_id, channel):
                assert project_id == "p"
                return StubConn()

        monkeypatch.setattr(
            channel_pause,
            "AccessModelRepository",
            lambda: StubRepo(),
        )

        auth = _scope_auth(scope_id="real-scope-id")
        with pytest.raises(HTTPException) as exc:
            channel_pause.enforce_channel_pause(auth, "cli")
        assert exc.value.status_code == 403

    def test_channel_pause_passes_when_connector_active(self, monkeypatch):
        from src.version_engine.admission import channel_pause
        channel_pause._channel_pause_cache.clear()

        class StubConn:
            id = "connector-1"
            status = "active"

        monkeypatch.setattr(
            channel_pause,
            "AccessModelRepository",
            lambda: type("R", (), {
                "get_by_target_kind": lambda self, p, s, c: StubConn(),
            })(),
        )

        auth = _scope_auth(scope_id="real-scope-id")
        channel_pause.enforce_channel_pause(auth, "cli")  # no raise

    def test_channel_pause_skips_unknown_channels(self):
        from src.version_engine.admission import channel_pause
        auth = _scope_auth(scope_id="real-scope-id")
        # Should NOT consult connector repo at all.
        channel_pause.enforce_channel_pause(auth, "webhook-xyz")
        channel_pause.enforce_channel_pause(auth, None)


# ════════════════════════════════════════════════════════════════
# I. Permission: read-only mode rejects writes
# ════════════════════════════════════════════════════════════════


class TestPermission:
    def test_read_only_facade_rejects_write(self):
        facade = RepoFacade(
            project_id="p", repo_id="s", kind="access_point",
            scope_path="", excludes=(), mode="r",
        )
        with pytest.raises(HTTPException) as exc:
            ensure_repo_writable(facade)
        assert exc.value.status_code == 403

    def test_read_only_facade_allows_read(self):
        facade = RepoFacade(
            project_id="p", repo_id="s", kind="access_point",
            scope_path="", excludes=(), mode="r",
        )
        ensure_repo_readable(facade)  # no raise

    def test_rw_facade_allows_both(self):
        facade = RepoFacade(
            project_id="p", repo_id="s", kind="access_point",
            scope_path="", excludes=(), mode="rw",
        )
        ensure_repo_readable(facade)
        ensure_repo_writable(facade)

    def test_target_admission_factory_runs_mode_check(self, monkeypatch):
        """The new TargetAdmission factory must gate by action."""
        from src.version_engine.admission import channel_pause
        monkeypatch.setattr(channel_pause, "_KNOWN_CHANNELS", frozenset())

        facade = RepoFacade(
            project_id="p", repo_id="s", kind="access_point",
            scope_path="", excludes=(), mode="r",
        )
        auth = _scope_auth()

        # Read action OK.
        admission = admit_target(
            auth, facade, action="read", source_channel="papi",
        )
        assert admission.allows("read")
        assert not admission.allows("write")

        # Write action refused.
        with pytest.raises(HTTPException) as exc:
            admit_target(auth, facade, action="write", source_channel="papi")
        assert exc.value.status_code == 403

    def test_target_admission_write_implies_read(self, monkeypatch):
        from src.version_engine.admission import channel_pause
        monkeypatch.setattr(channel_pause, "_KNOWN_CHANNELS", frozenset())

        facade = RepoFacade(
            project_id="p", repo_id="s", kind="access_point",
            scope_path="", excludes=(), mode="rw",
        )
        auth = _scope_auth()
        admission = admit_target(auth, facade, action="write", source_channel="papi")
        assert admission.allows("write")
        assert admission.allows("read")
        assert admission.allows("delete")

    def test_target_admission_unknown_action_refused(self, monkeypatch):
        from src.version_engine.admission import channel_pause
        monkeypatch.setattr(channel_pause, "_KNOWN_CHANNELS", frozenset())

        facade = RepoFacade(
            project_id="p", repo_id="s", kind="access_point",
            scope_path="", excludes=(), mode="rw",
        )
        with pytest.raises(HTTPException) as exc:
            admit_target(
                _scope_auth(), facade,
                action="invalid_xyz", source_channel="papi",
            )
        assert exc.value.status_code == 400


# ════════════════════════════════════════════════════════════════
# J. Shadow snapshot caps (entry count → 413)
# ════════════════════════════════════════════════════════════════
#
# The previous "manifest JSON byte size" cap was retired when the
# manifest moved from a Supabase JSONB column to S3 (one object per
# snapshot). S3 has no relevant size ceiling for our purposes, so the
# remaining caps are sanity bounds: entry count (so the server isn't
# DoS'd by a 10M-row manifest) and per-file size (so individual blob
# upload paths stay sane). Both raise HTTPException(413) directly now;
# no domain-specific exception type to import.


class TestShadowSnapshotCaps:
    def _entry(self, **overrides):
        from src.version_engine.entrypoints.http.shadow_snapshot import (
            ShadowSnapshotEntry,
        )
        defaults = {"path": "a.txt", "blob_hash": "a" * 40}
        defaults.update(overrides)
        return ShadowSnapshotEntry(**defaults)

    def _request(self, manifest):
        from src.version_engine.entrypoints.http.shadow_snapshot import (
            UpsertShadowSnapshotRequest,
        )
        return UpsertShadowSnapshotRequest(
            project_id="test-proj", manifest=manifest,
        )

    def test_small_manifest_passes(self):
        from src.version_engine.entrypoints.http.shadow_snapshot import (
            _enforce_entry_count,
        )
        req = self._request([self._entry()])
        _enforce_entry_count(req)  # no raise

    def test_oversize_entry_count_raises_413(self):
        from fastapi import HTTPException

        from src.version_engine.entrypoints.http.shadow_snapshot import (
            _MAX_FILES_PER_SNAPSHOT,
            _enforce_entry_count,
        )
        # Build a manifest one entry past the cap. Use unique paths so
        # pydantic doesn't reject duplicates before we hit the cap check.
        oversize = [
            self._entry(path=f"f{i}.txt") for i in range(_MAX_FILES_PER_SNAPSHOT + 1)
        ]
        req = self._request(oversize)
        with pytest.raises(HTTPException) as exc:
            _enforce_entry_count(req)
        assert exc.value.status_code == 413
        # Detail body names which cap was hit so the client can decide
        # to split / skip / upgrade.
        detail = exc.value.detail
        assert detail["limit"] == "manifest entry count"
        assert detail["actual"] > detail["cap"]

    def test_entry_traversal_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._entry(path="../etc/passwd")

    def test_submodule_mode_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._entry(mode="160000")  # submodule

    def test_oversize_per_file_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._entry(size=200 * 1024 * 1024)  # 200 MB > 100 MB cap


# ════════════════════════════════════════════════════════════════
# K. policy_override on VersionSubmissionIntent (Git push manual_review)
# ════════════════════════════════════════════════════════════════


class TestPolicyOverridePropagation:
    @pytest.mark.asyncio
    async def test_submission_policy_override_routes_to_manual_review(
        self, repo_manager, server_repo,
    ):
        """The new policy_override field on VersionSubmissionIntent must
        actually flip the CAS-retry merge to manual_review."""
        await _seed_scope(server_repo, repo_manager, "", {"x.txt": b"v0"})
        conflict_table = _FakeConflictTable()
        engine = VersionWriteEngine(repo_manager, conflict_table)
        base_head = server_repo.get_scope_head_commit_id("")

        # First writer lands.
        a_tree = build_tree_from_files(server_repo.store, {"x.txt": b"alice"})
        a_commit = _make_commit(server_repo, a_tree, message="alice", parent=base_head)
        await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:alice",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=a_tree, client_commit_id=a_commit,
            proposed_files={"x.txt": b"alice"}, message="alice",
        ))

        # Second writer collides; opts into manual_review via the new field.
        b_tree = build_tree_from_files(server_repo.store, {"x.txt": b"bob"})
        b_commit = _make_commit(server_repo, b_tree, message="bob", parent=base_head)
        b_result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj", scope_path="", actor="user:bob",
            source_channel="papi", base_commit_id=base_head,
            proposed_tree_id=b_tree, client_commit_id=b_commit,
            proposed_files={"x.txt": b"bob"}, message="bob",
            policy_override="manual_review",
        ))
        assert b_result.status == "pending"
        # Without policy_override default would be LWW → "ok".

    def test_rollback_intent_accepts_policy_override(self):
        """Just verifying the dataclass has the field — the engine wiring
        for rollback isn't tested here because rollback flow is bigger."""
        intent = RollbackIntent(
            project_id="p", scope_path="docs", actor="user:a",
            source_channel="papi", target_commit_id="abc",
            policy_override="manual_review",
        )
        assert intent.policy_override == "manual_review"


# ════════════════════════════════════════════════════════════════
# M. initialize_project_tree idempotency
# ════════════════════════════════════════════════════════════════


class TestInitializeProjectTree:
    @pytest.mark.asyncio
    async def test_init_on_fresh_project_sets_empty_tree(
        self, repo_manager, server_repo,
    ):
        # ``initialize_project_tree`` uses ``get_repo`` (the read-side
        # admin facade); the shared fixture only wires ``get_server_repo``.
        # Point both at the same in-memory repo.
        repo_manager.get_repo.return_value = server_repo

        assert server_repo.history.get_root_hash() == ""

        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())
        root = await engine.initialize_project_tree("test-proj")
        assert root == EMPTY_TREE_SHA1
        assert server_repo.history.get_root_hash() == EMPTY_TREE_SHA1

    @pytest.mark.asyncio
    async def test_init_idempotent_on_existing_empty(
        self, repo_manager, server_repo,
    ):
        repo_manager.get_repo.return_value = server_repo
        engine = VersionWriteEngine(repo_manager, _FakeConflictTable())
        await engine.initialize_project_tree("test-proj")
        # Second call must return EMPTY_TREE_SHA1 unchanged.
        root = await engine.initialize_project_tree("test-proj")
        assert root == EMPTY_TREE_SHA1


# ════════════════════════════════════════════════════════════════
# N. Health endpoint payload + recommended_actions for all four states
# ════════════════════════════════════════════════════════════════


class TestProductOperationAdapterE2E:
    """End-to-end through L4 ``ProductOperationAdapter`` — the path
    frontend ``Save`` button traverses. Each test stubs
    ``get_project_write_state`` (the one DB-RPC dependency) and asserts
    the full L4→L5→L6 chain lands a commit and reflects in the read path.
    """

    def _wire_write_state(self, repo_manager, server_repo):
        from src.version_engine.domain.intents import ProjectWriteState
        # The reader uses ``get_repo``; the write path uses
        # ``get_server_repo``. Conftest wires the latter; we add the
        # former so the ProductOperationAdapter's ``self._reader`` works
        # against the same in-memory repo.
        repo_manager.get_repo.return_value = server_repo
        # Re-read root/head AFTER any prior writes so the write_state
        # snapshot we feed the engine matches the current canonical
        # state (otherwise the engine raises ConcurrentMutationError on
        # the second call).
        repo_manager.get_project_write_state.return_value = ProjectWriteState(
            project_id="test-proj", project_name="Test", org_id="org-1",
            visibility="private", role="owner", can_write=True,
            root_hash=server_repo.history.get_root_hash(),
            head_commit_id=server_repo.history.get_head_commit_id(),
        )

    @pytest.mark.asyncio
    async def test_product_write_then_read(self, repo_manager, server_repo):
        """Frontend save → ProductOperationAdapter.write_file → file
        visible via VersionTreeReader."""
        self._wire_write_state(repo_manager, server_repo)
        ops = ProductOperationAdapter(repo_manager)

        result = await ops.write_file(
            project_id="test-proj",
            path="notes/hello.md",
            content=b"# Hello world\n",
            who="user:alice",
            message="first write",
        )
        assert result.commit_id

        # Read back via the reader.
        content = ops._reader.read_file("test-proj", "notes/hello.md")
        assert content == b"# Hello world\n"

    @pytest.mark.asyncio
    async def test_product_bulk_write_atomic(self, repo_manager, server_repo):
        """bulk_write commits multiple files in ONE commit."""
        self._wire_write_state(repo_manager, server_repo)
        ops = ProductOperationAdapter(repo_manager)

        result = await ops.bulk_write(
            project_id="test-proj",
            files={"a.md": b"A", "b.md": b"B", "c.md": b"C"},
            who="user:alice",
            message="three files",
        )
        assert result.commit_id

        # All three live, single commit_id on the audit row.
        for name, body in [("a.md", b"A"), ("b.md", b"B"), ("c.md", b"C")]:
            assert ops._reader.read_file("test-proj", name) == body

        # History has one commit for the bulk, not three.
        entries = server_repo.history.get_since("", limit=10)
        assert len(entries) == 1, f"bulk_write should be one commit, got {len(entries)}"

    @pytest.mark.asyncio
    async def test_product_delete_removes_path(self, repo_manager, server_repo):
        self._wire_write_state(repo_manager, server_repo)
        ops = ProductOperationAdapter(repo_manager)

        await ops.write_file("test-proj", "tmp.txt", b"throwaway", "user:a", message="add")
        assert ops._reader.read_file("test-proj", "tmp.txt") == b"throwaway"

        # Re-stub write_state with the now-updated head_commit_id so
        # the engine doesn't trip ConcurrentMutationError (the stubbed
        # ProjectWriteState carries head_commit_id="" which races).
        self._wire_write_state(repo_manager, server_repo)

        await ops.delete("test-proj", ["tmp.txt"], "user:a", message="remove")

        # File gone.
        with pytest.raises(FileNotFoundError):
            ops._reader.read_file("test-proj", "tmp.txt")


class TestBatchAdapterThirdParty:
    """Third-party / connector / sync flow — uses the in-process batch
    client to exercise the same engine path as a real Git push without
    HTTP. Doc map: ``[B] Batch/internal tool`` row in the L0–L6
    diagram → ``adapters/batch/in_process_client.py``.
    """

    def _client(self, repo_manager, scope_path="", excludes=()):
        from src.platform.repository_target.models import RepositoryPathProjection
        from src.version_engine.adapters.batch.in_process_client import (
            InProcessVersionClient,
        )
        return InProcessVersionClient(
            repo_manager,
            project_id="test-proj",
            projection=RepositoryPathProjection(
                path_prefix=scope_path,
                excludes=tuple(excludes),
            ),
            actor="sync:gmail-connector",
        )

    def test_connector_initial_push(self, repo_manager, server_repo):
        """Cold project + connector pushes some files."""
        client = self._client(repo_manager)
        # Clone empty (no commits yet).
        files = client.clone()
        assert files == {}

        # Push connector content.
        client.push(
            modified={"inbox/msg1.eml": b"From: a@x\n", "inbox/msg2.eml": b"From: b@y\n"},
            deleted=[],
            message="sync inbox",
        )

        # Reflected in the canonical store.
        scope_files = _scope_files(server_repo, "")
        assert scope_files.get("inbox/msg1.eml") == b"From: a@x\n"
        assert scope_files.get("inbox/msg2.eml") == b"From: b@y\n"

    def test_connector_incremental_push_with_delete(
        self, repo_manager, server_repo,
    ):
        """AccessSurface adds + removes paths in one sync round."""
        client = self._client(repo_manager)
        client.push(
            modified={"a.eml": b"A", "b.eml": b"B", "c.eml": b"C"},
            deleted=[],
            message="initial",
        )
        client.push(
            modified={"d.eml": b"D"},
            deleted=["a.eml"],
            message="incremental",
        )
        files = _scope_files(server_repo, "")
        assert "a.eml" not in files, "deleted path should be gone"
        assert files["b.eml"] == b"B"
        assert files["d.eml"] == b"D"


class TestHealthPayload:
    def _payload_for_state(self, state, monkeypatch):
        """Stub resolve_git_view_head to force a state and capture payload."""
        from src.version_engine.adapters.git import health
        from src.version_engine.adapters.git.view_projection import GitViewHead

        monkeypatch.setattr(
            health,
            "resolve_git_view_head",
            lambda repo, scope, excludes: GitViewHead(
                head="" if state == "empty" else "a" * 40,
                canonical_head="" if state == "empty" else "a" * 40,
                health=state,
                history_cut=(state == "history_degraded"),
                reason="",
            ),
        )
        return health.git_view_health_payload(
            None, project_id="p", scope_path="", scope_excludes=[],
        )

    def test_empty_state_returns_first_commit_action(self, monkeypatch):
        payload = self._payload_for_state("empty", monkeypatch)
        actions = payload["recommended_actions"]
        assert any(a["type"] == "first_commit" for a in actions), actions
        assert payload["git_usable"] is True

    def test_healthy_state_returns_none_action(self, monkeypatch):
        payload = self._payload_for_state("healthy", monkeypatch)
        assert payload["recommended_actions"] == [
            {"type": "none", "label": "No action required — view is healthy"},
        ]
        assert payload["git_usable"] is True
        assert payload["push_usable"] is True

    def test_history_degraded_returns_continue_and_repair(self, monkeypatch):
        payload = self._payload_for_state("history_degraded", monkeypatch)
        actions = payload["recommended_actions"]
        types = {a["type"] for a in actions}
        assert "continue" in types and "repair_history" in types
        assert payload["history_cut"] is True
        assert payload["git_usable"] is True  # Git still usable

    def test_current_corrupt_blocks_git(self, monkeypatch):
        payload = self._payload_for_state("current_corrupt", monkeypatch)
        actions = payload["recommended_actions"]
        types = {a["type"] for a in actions}
        # Three actions per the new spec including rebuild_cache.
        assert {"restore_version", "repair_storage", "rebuild_cache"} <= types
        assert payload["git_usable"] is False
        assert payload["push_usable"] is False
