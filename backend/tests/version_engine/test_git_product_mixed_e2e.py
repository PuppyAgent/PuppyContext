from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Header

from src.version_engine.entrypoints.git.router import router as git_router
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.bootstrap.dependencies import get_repo_manager, get_version_write_command_service
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.entrypoints.http.content_write import write_router
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from tests.version_engine.test_write_engine import (
    _configure_git_identity,
    _files_for_scope,
    _patch_git_scope_auth,
    _run_git,
    _run_git_raw,
    _serve_git_app,
)
from tests.version_engine.test_server_repo import FakeAuditManager, FakeHistoryManager


FRONTEND_HTTP_SAVE_BUDGET_MS = 2_000

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="concurrent real-git HTTP test deadlocks in Windows msvcrt file locks; Linux CI covers it",
)


@pytest.fixture
def memory_store(tmp_path) -> ObjectStore:
    obj_dir = tmp_path / "objects"
    obj_dir.mkdir()
    return ObjectStore(obj_dir)


def test_git_cli_and_frontend_native_writes_share_version_engine_under_concurrency(
    monkeypatch,
    tmp_path,
    memory_store,
):
    # This is an in-memory mixed-protocol test. Isolate the three optional
    # control/derived seams that otherwise read the developer's Supabase URL
    # from .env and leak slow background threads into the test.
    from src.platform.access.model_repository import AccessModelRepository
    from src.version_engine.derived import outbox
    from src.infra.search import text_indexer
    from src.version_engine.infrastructure.supabase.version_ref_repository import (
        VersionRefStore,
    )

    monkeypatch.setattr(
        AccessModelRepository, "get_by_scope_kind", lambda *_a, **_k: None
    )
    monkeypatch.setattr(VersionRefStore, "list_refs", lambda *_a, **_k: [])
    monkeypatch.setattr(text_indexer, "index_commit_delta", lambda *_a, **_k: None)
    monkeypatch.setattr(outbox, "complete_version_outbox_for_commit", lambda *_a, **_k: True)

    from src.version_engine.write_engine.tree_objects import build_tree_from_files
    from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager
    from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo

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

    server_repo = PuppyOneServerRepo(
        project_id="test-proj",
        project_name="Test Project",
        store=memory_store,
        history=FakeHistoryManager(),
        audit=FakeAuditManager(),
        scopes=ScopeManager(FakeScopeBackend()),
    )
    server_repo.add_scope("docs-scope", "/docs/")

    empty_tree = build_tree_from_files(server_repo.store, {})
    server_repo.history.set_root_hash(empty_tree)
    server_repo.history.set_scope_hash("docs", empty_tree)

    repo_manager = MagicMock(spec=VersionRepoManager)
    repo_manager.get_native_service.return_value = None  # This fixture retains legacy Scope authority.
    repo_manager.get_server_repo.return_value = server_repo

    def fake_project_write_state(project_id, user_id):
        assert project_id == "test-proj"
        assert user_id in {"frontend-alice", "frontend-bob"}
        return ProjectWriteState(
            project_id=project_id,
            project_name="Test Project",
            role="editor",
            can_write=True,
            root_hash=server_repo.history.get_root_hash(),
            head_commit_id=server_repo.history.get_scope_head_commit_id("") or "",
        )

    repo_manager.get_project_write_state.side_effect = fake_project_write_state
    ops = ProductOperationAdapter(repo_manager)

    _patch_git_scope_auth(
        monkeypatch,
        {
            "git-alice-key": ("git-alice", "/docs/", "rw"),
            "git-bob-key": ("git-bob", "/docs/", "rw"),
            "verify-key": ("verify", "/docs/", "rw"),
        },
    )

    async def current_user_override(
        x_test_user: str = Header(default="frontend-alice"),
    ):
        return CurrentUser(user_id=x_test_user, role="authenticated")

    app = FastAPI()
    app.include_router(git_router)
    app.include_router(write_router, prefix="/api/v1/content")
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager
    app.dependency_overrides[get_version_write_command_service] = (
        lambda: VersionWriteCommandService(ops)
    )
    app.dependency_overrides[get_current_user] = current_user_override

    with _serve_git_app(app) as base_url:
        alice_remote = f"{base_url}/git/ap/git-alice-key.git"
        bob_remote = f"{base_url}/git/ap/git-bob-key.git"
        verify_remote = f"{base_url}/git/ap/verify-key.git"
        content_url = f"{base_url}/api/v1/content/test-proj/write"

        alice_repo = tmp_path / "git-alice"
        bob_repo = tmp_path / "git-bob"
        verify_repo = tmp_path / "verify"

        _run_git(["clone", alice_remote, str(alice_repo)], tmp_path)
        _run_git(["clone", bob_remote, str(bob_repo)], tmp_path)
        _configure_git_identity(alice_repo)
        _configure_git_identity(bob_repo)

        _run_git(["config", "user.name", "Git Alice"], alice_repo)
        _run_git(["config", "user.email", "git-alice@example.com"], alice_repo)
        (alice_repo / "git-alice.md").write_text("from git alice\n", encoding="utf-8")
        _run_git(["add", "git-alice.md"], alice_repo)
        _run_git(["commit", "-m", "git alice writes"], alice_repo)

        _run_git(["config", "user.name", "Git Bob"], bob_repo)
        _run_git(["config", "user.email", "git-bob@example.com"], bob_repo)
        (bob_repo / "git-bob.md").write_text("from git bob\n", encoding="utf-8")
        _run_git(["add", "git-bob.md"], bob_repo)
        _run_git(["commit", "-m", "git bob writes"], bob_repo)

        barrier = threading.Barrier(4)

        def timed(kind: str, actor: str, fn):
            barrier.wait(timeout=10)
            started = time.perf_counter()
            payload = fn()
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            return {
                "kind": kind,
                "actor": actor,
                "elapsed_ms": elapsed_ms,
                "payload": payload,
            }

        def push_git(actor: str, repo_dir):
            def run():
                proc = None
                for _attempt in range(4):
                    proc = _run_git_raw(["push", "origin", "main"], repo_dir)
                    if proc.returncode == 0:
                        break
                    stderr = proc.stderr.decode("utf-8", errors="replace")
                    if (
                        "non-fast-forward" not in stderr
                        and "fetch first" not in stderr
                    ):
                        raise AssertionError(stderr)
                    _run_git(["fetch", "origin"], repo_dir)
                    _run_git(["rebase", "origin/main"], repo_dir)
                if proc is None or proc.returncode != 0:
                    stderr = (
                        proc.stderr.decode("utf-8", errors="replace")
                        if proc is not None else "push did not run"
                    )
                    raise AssertionError(stderr)
                return {
                    "stderr": proc.stderr.decode("utf-8", errors="replace"),
                    "head": _run_git(["rev-parse", "HEAD"], repo_dir)
                    .decode("ascii")
                    .strip(),
                }

            return timed("git_cli", actor, run)

        def frontend_write(actor: str, path: str, content: str):
            def run():
                with httpx.Client(timeout=20, trust_env=False) as client:
                    response = client.post(
                        content_url,
                        json={
                            "path": path,
                            "content": content,
                            "node_type": "markdown",
                            "message": f"{actor} writes",
                        },
                        headers={"X-Test-User": actor},
                    )
                if response.status_code != 200:
                    raise AssertionError(
                        f"frontend write failed: {response.status_code} "
                        f"{response.text}"
                    )
                return response.json()

            return timed("frontend_http", actor, run)

        total_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(push_git, "git-alice", alice_repo),
                pool.submit(push_git, "git-bob", bob_repo),
                pool.submit(
                    frontend_write,
                    "frontend-alice",
                    "docs/frontend-alice",
                    "from frontend alice\n",
                ),
                pool.submit(
                    frontend_write,
                    "frontend-bob",
                    "docs/frontend-bob",
                    "from frontend bob\n",
                ),
            ]
            results = [future.result(timeout=30) for future in futures]
        total_elapsed_ms = round((time.perf_counter() - total_started) * 1000, 2)

        _run_git(["clone", verify_remote, str(verify_repo)], tmp_path)

    expected_files = {
        "frontend-alice.md": b"from frontend alice\n",
        "frontend-bob.md": b"from frontend bob\n",
        "git-alice.md": b"from git alice\n",
        "git-bob.md": b"from git bob\n",
    }
    assert _files_for_scope(server_repo, "docs") == expected_files
    for name, body in expected_files.items():
        assert (verify_repo / name).read_bytes() == body

    audit_types = [event["type"] for event in server_repo.audit.events]
    assert audit_types.count("access_git_push") == 2
    assert audit_types.count("write_file") == 2
    user_audit_events = [
        event for event in server_repo.audit.events
        if event["type"] in {"access_git_push", "write_file"}
    ]
    user_history_entries = [
        entry for entry in server_repo.history._entries
        if entry.get("audit_event_type") != "projection"
    ]
    assert len(user_history_entries) == 4
    history_scopes_by_type = [
        (entry["message"], entry["scope_path"])
        for entry in user_history_entries
    ]

    for event in user_audit_events:
        detail = event["detail"]
        assert detail["commit_id"]
        assert detail["scope_hash"]
        assert detail["cas_attempts"] >= 1
        assert detail["changes"] >= 1
        if event["type"] == "write_file":
            assert detail["scope"] == ""
            assert detail["project_root_operation"] is True
            assert detail["root_hash"] == detail["scope_hash"]
        else:
            assert event["type"] == "access_git_push"
            assert detail["scope"] == "docs"
            assert "project_root_operation" not in detail

    assert sorted(
        scope for message, scope in history_scopes_by_type if "frontend-" in message
    ) == ["", ""]
    assert sorted(
        scope for message, scope in history_scopes_by_type if "git " in message
    ) == ["docs", "docs"]

    frontend_operation_ms = [
        item["elapsed_ms"] for item in results if item["kind"] == "frontend_http"
    ]
    max_frontend_operation_ms = max(frontend_operation_ms)
    max_single_operation_ms = max(item["elapsed_ms"] for item in results)
    assert max_frontend_operation_ms < FRONTEND_HTTP_SAVE_BUDGET_MS
    assert max_single_operation_ms < 20_000
    assert total_elapsed_ms < 30_000

    print(
        "mixed_git_frontend_e2e_metrics="
        + json.dumps(
            {
                "operations": [
                    {
                        "kind": item["kind"],
                        "actor": item["actor"],
                        "elapsed_ms": item["elapsed_ms"],
                    }
                    for item in results
                ],
                "frontend_budget_ms": FRONTEND_HTTP_SAVE_BUDGET_MS,
                "max_frontend_operation_ms": max_frontend_operation_ms,
                "max_single_operation_ms": max_single_operation_ms,
                "total_elapsed_ms": total_elapsed_ms,
                "history_entries": len(server_repo.history._entries),
                "audit_types": audit_types,
                "final_files": sorted(expected_files),
            },
            sort_keys=True,
        )
    )
