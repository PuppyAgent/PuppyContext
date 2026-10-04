"""Git-native transaction-engine contract tests.

These tests mirror the important version submission semantics from the perspective of
real Git objects: commits, trees, blobs, and scoped refs. They intentionally
exercise the new transaction core directly rather than routing through the
version adapter.
"""

from __future__ import annotations

import base64
import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from src.config import settings
from src.version_engine.write_engine import tree as tree_mod
from src.version_engine.write_engine.git_object_format import decode_commit, encode_commit
from src.version_engine.storage.object_store import ObjectStore

# Sentinel preserved so older tests that send a request body keep working;
# the engine no longer reads a wire-protocol version.
PROTOCOL_VERSION = 1
from pydantic import ValidationError

from src.version_engine.bootstrap.dependencies import get_repo_manager
from src.version_engine.infrastructure.supabase.db_names import (
    CLAIM_OUTBOX_RPC,
    COMPLETE_OUTBOX_RPC,
    FAIL_OUTBOX_RPC,
)
from src.version_engine.adapters.git.submission import submit_git_tree
from src.version_engine.adapters.git.object_quarantine import (
    copy_reachable_objects_to_bare,
    quarantine_bare_repo,
    receive_pack_advertisement_bare_repo,
    transport_bare_repo,
)
from src.version_engine.entrypoints.git.router import router as git_router
from src.version_engine.adapters.git.protocol import run_git
from src.version_engine.adapters.git.view_projection import git_view_head_commit
from src.version_engine.write_engine.engine import (
    NonFastForwardSubmissionError,
    VersionWriteEngine,
)
from src.version_engine.domain.intents import RollbackIntent, VersionSubmissionIntent
from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.auth_context import repository_view_from_auth
from src.platform.repository_target.models import (
    ProjectRootTarget,
    ResolvedRepositoryView,
    ScopeTarget,
)


@pytest.fixture(autouse=True)
def _use_hard_receive_cap_without_live_entitlement_lookup(monkeypatch):
    """Git protocol tests use explicit control-plane doubles, never a live DB."""
    monkeypatch.setattr(
        "src.version_engine.infrastructure.supabase.version_ref_repository.VersionRefStore.list_refs",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router._git_receive_max_body_bytes",
        lambda _project_id: int(settings.GIT_MAX_RECEIVE_PACK_BYTES),
    )


async def submit_agent_push(repo_manager, project_id, auth, body):
    """In-process adapter used by older tests after the version wire-protocol
    surface was removed. Translates the old request body into the canonical
    intent so engine tests written against the version adapter still cover the
    publish path.
    """
    repo = repo_manager.get_server_repo(project_id)
    view = repository_view_from_auth(auth)
    for object_id, b64data in (body.get("objects") or {}).items():
        repo.store.put_loose(object_id, base64.b64decode(b64data))
    snapshot = body["snapshots"][-1]
    engine = VersionWriteEngine(repo_manager)
    result = await engine.submit_version(VersionSubmissionIntent(
        project_id=project_id,
        scope_path=view.path_prefix,
        actor=auth.get("agent", ""),
        source_channel="agent",
        base_commit_id=body.get("base_commit_id", "") or "",
        proposed_tree_id=snapshot["root"],
        client_commit_id=snapshot.get("commit_id", ""),
        message=snapshot.get("message", ""),
        scope_excludes=list(view.excludes),
        audit_detail={"snapshots": len(body.get("snapshots", []))},
        defer_projection=True,
    ))
    return {
        "status": result.status,
        "commit_id": result.commit_id,
        "pushed": len(body.get("snapshots", [])),
        "root": result.new_scope_hash,
        "merged": result.merged,
        "conflicts": result.conflicts,
        "merged_changes": result.merged_changes,
        "commit_object": result.commit_object,
    }


async def submit_version_rollback(repo_manager, project_id, auth, body):
    view = repository_view_from_auth(auth)
    engine = VersionWriteEngine(repo_manager)
    result = await engine.rollback(RollbackIntent(
        project_id=project_id,
        scope_path=view.path_prefix,
        actor=auth.get("agent", ""),
        source_channel="agent",
        target_commit_id=body["target_commit_id"],
        message=body.get("message", ""),
        scope_excludes=list(view.excludes),
        defer_projection=True,
    ))
    return {
        "status": result.status,
        "new_commit_id": result.commit_id,
        "target_commit_id": body["target_commit_id"],
    }
from src.version_engine.write_engine.git_commit import (
    GitCommitInvariantError,
    build_git_commit,
    commit_tree_id,
    identity_for_git,
    is_git_compatible_commit,
)
from src.version_engine.write_engine.engine import (
    CrossScopeSubmissionError,
    VersionWriteEngine,
)
from src.version_engine.write_engine.tree_objects import build_tree_from_files, flatten_tree_to_bytes
from src.version_engine.domain.intents import OperationWriteIntent, ProjectWriteState
import src.version_engine.derived.hooks as derived_hooks
from src.version_engine.derived.outbox import process_version_outbox_batch
from src.version_engine.adapters.product.tree_patch import splice_put_blob
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager

from tests.version_engine.test_server_repo import FakeAuditManager, FakeHistoryManager


@pytest.fixture
def memory_store(tmp_path) -> ObjectStore:
    obj_dir = tmp_path / "objects"
    obj_dir.mkdir()
    return ObjectStore(obj_dir)


@pytest.fixture
def server_repo(memory_store):
    from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager
    from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo

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
        store=memory_store,
        history=history,
        audit=audit,
        scopes=ScopeManager(FakeScopeBackend()),
    )


@pytest.fixture
def repo_manager(server_repo):
    manager = MagicMock(spec=VersionRepoManager)
    manager.get_server_repo.return_value = server_repo
    return manager


def _git_time() -> str:
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return f"{int(dt.timestamp())} +0000"


def _make_client_commit(repo, tree_id: str, parent_id: str = "", message: str = "git push") -> str:
    identity = identity_for_git("git:user")
    body = encode_commit(
        tree_sha1=tree_id,
        parent_sha1=parent_id or None,
        author=identity,
        author_time=_git_time(),
        committer=identity,
        committer_time=_git_time(),
        message=message,
    )
    return repo.store.put_commit(body)


def _files_for_scope(repo, scope: str = "") -> dict[str, bytes]:
    tree_id = repo.get_scope_hash(scope)
    return flatten_tree_to_bytes(repo.store, tree_id)


def _last_audit_event(repo, event_type: str) -> dict:
    for event in reversed(repo.audit.events):
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"missing audit event {event_type!r}")


def _init_empty_project_shell(repo) -> str:
    empty_tree = build_tree_from_files(repo.store, {})
    repo.history.set_root_hash(empty_tree)
    return empty_tree


@pytest.mark.asyncio
async def test_project_operation_reuses_coalesced_write_state(
    repo_manager,
    server_repo,
):
    base_root = _init_empty_project_shell(server_repo)
    write_state = ProjectWriteState(
        project_id="test-proj",
        project_name="State Loaded Project",
        role="owner",
        can_write=True,
        root_hash=base_root,
        head_commit_id="",
    )

    def fail_live_root_read():
        raise AssertionError("project write should reuse ProjectWriteState root")

    def fail_live_head_read(_scope_path):
        raise AssertionError("project write should reuse ProjectWriteState head")

    server_repo.history.get_root_hash = fail_live_root_read
    server_repo.history.get_scope_state = fail_live_head_read

    engine = VersionWriteEngine(repo_manager)
    result = await engine.apply_project_operation(
        OperationWriteIntent(
            project_id="test-proj",
            scope_path="",
            actor="user:save",
            source_channel="papi",
            operation_type="write_file",
            message="save",
            audit_detail={"path": "hello.md"},
            project_write_state=write_state,
        ),
        lambda store, root: splice_put_blob(store, root, "hello.md", b"hello"),
    )

    assert result.status == "ok"
    assert repo_manager.get_server_repo.call_args.kwargs["project_name"] == (
        "State Loaded Project"
    )


@pytest.mark.asyncio
async def test_git_fast_forward_push_uses_tree_diff_not_full_flatten(
    repo_manager,
    server_repo,
    monkeypatch,
):
    server_repo.add_scope("docs-scope", "/docs/")
    base_tree = build_tree_from_files(server_repo.store, {"a.md": b"a\n"})
    base_commit = _make_client_commit(server_repo, base_tree, message="base")
    next_tree = build_tree_from_files(server_repo.store, {"a.md": b"b\n"})
    next_commit = _make_client_commit(
        server_repo,
        next_tree,
        parent_id=base_commit,
        message="next",
    )
    server_repo.history.set_scope_hash("docs", base_tree)
    server_repo.set_scope_head_commit_id("docs", base_commit)

    def fail_flatten(*args, **kwargs):
        raise AssertionError("git fast-forward path must not flatten whole trees")

    monkeypatch.setattr(
        "src.version_engine.write_engine.submission_writer._scope_files_for_head",
        fail_flatten,
    )

    result = await submit_git_tree(
        repo_manager,
        project_id="test-proj",
        scope_path="docs",
        actor="git:user",
        base_commit_id=base_commit,
        proposed_tree_id=next_tree,
        client_commit_id=next_commit,
        message="next",
        changed_paths=["a.md"],
        defer_projection=True,
    )

    # Root-first scoped Git writes publish a canonical project-root commit
    # while keeping the client's commit as the scope/AP Git head.
    assert result.commit_id != next_commit
    assert server_repo.get_scope_head_commit_id("docs") == next_commit
    assert result.changes == [{"path": "docs/a.md", "action": "update"}]
    assert server_repo.get_scope_hash("docs") == next_tree
    assert server_repo.get_root_hash() != next_tree


@pytest.mark.asyncio
async def test_stale_git_push_rejects_before_sparse_merge(
    repo_manager,
    server_repo,
    monkeypatch,
):
    server_repo.add_scope("docs-scope", "/docs/")
    base_tree = build_tree_from_files(server_repo.store, {
        "a.md": b"base\n",
        "keep.md": b"same\n",
    })
    base_commit = _make_client_commit(server_repo, base_tree, message="base")
    await submit_git_tree(
        repo_manager,
        project_id="test-proj",
        scope_path="docs",
        actor="git:user",
        base_commit_id="",
        proposed_tree_id=base_tree,
        client_commit_id=base_commit,
        message="base",
        changed_paths=["a.md", "keep.md"],
        defer_projection=True,
    )

    server_tree = build_tree_from_files(server_repo.store, {
        "a.md": b"base\n",
        "keep.md": b"same\n",
        "server.md": b"server-only\n",
    })
    server_commit = _make_client_commit(
        server_repo,
        server_tree,
        parent_id=base_commit,
        message="server",
    )
    await submit_git_tree(
        repo_manager,
        project_id="test-proj",
        scope_path="docs",
        actor="git:user",
        base_commit_id=base_commit,
        proposed_tree_id=server_tree,
        client_commit_id=server_commit,
        message="server",
        changed_paths=["server.md"],
        defer_projection=True,
    )

    client_tree = build_tree_from_files(server_repo.store, {
        "a.md": b"client\n",
        "keep.md": b"same\n",
    })
    client_commit = _make_client_commit(
        server_repo,
        client_tree,
        parent_id=base_commit,
        message="client",
    )

    def fail_flatten(*args, **kwargs):
        raise AssertionError("stale Git push must reject before flattening trees")

    monkeypatch.setattr(
        "src.version_engine.write_engine.submission_writer._scope_files_for_head",
        fail_flatten,
    )

    with pytest.raises(NonFastForwardSubmissionError):
        await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="docs",
            actor="git:user",
            base_commit_id=base_commit,
            proposed_tree_id=client_tree,
            client_commit_id=client_commit,
            message="client",
            changed_paths=["a.md"],
            defer_projection=True,
        )

    assert _files_for_scope(server_repo, "docs") == {
        "a.md": b"base\n",
        "keep.md": b"same\n",
        "server.md": b"server-only\n",
    }


def _pkt_line(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


def _run_git(
    args: list[str],
    cwd,
    input_data: bytes | None = None,
    *,
    env: dict[str, str] | None = None,
) -> bytes:
    proc = _run_git_raw(args, cwd, input_data, env=env)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout


def _run_git_raw(
    args: list[str],
    cwd,
    input_data: bytes | None = None,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    # Force ``main`` as the local default branch regardless of the host
    # git config. Older / Windows git defaults to ``master`` which makes
    # every push test fail with ``src refspec main does not match any``
    # — the tests intentionally assert against the server's ``main`` ref
    # so the client must also be on ``main``.
    proc = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    return proc


def _configure_git_identity(repo_dir) -> None:
    _run_git(["config", "user.name", "Git Smoke"], repo_dir)
    _run_git(["config", "user.email", "git-smoke@example.com"], repo_dir)


def _standard_git_credential_env(tmp_path, token: str) -> dict[str, str]:
    """Use Git's credential-helper protocol without putting a secret in a URL."""

    helper = tmp_path / "git-credential-puppyone-test"
    helper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = get ]; then\n"
        "  printf 'username=x-puppyone-token\\npassword=%s\\n' "
        "\"$PUPPYONE_TEST_GIT_TOKEN\"\n"
        "fi\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    config = tmp_path / "gitconfig"
    _run_git(["config", "--file", str(config), "credential.helper", str(helper)], tmp_path)
    _run_git(["config", "--file", str(config), "credential.useHttpPath", "true"], tmp_path)
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_TERMINAL_PROMPT": "0",
        "PUPPYONE_TEST_GIT_TOKEN": token,
    }


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _serve_git_app(app: FastAPI):
    port = _free_tcp_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise AssertionError("Git smoke test server did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("Git smoke test server did not stop")


def _make_git_receive_pack_body(tmp_path) -> tuple[bytes, str]:
    work = tmp_path / "client"
    work.mkdir()
    _run_git(["init"], work)
    _run_git(["config", "user.name", "Git User"], work)
    _run_git(["config", "user.email", "git@example.com"], work)
    (work / "README.md").write_text("hello from git\n", encoding="utf-8")
    _run_git(["add", "README.md"], work)
    _run_git(["commit", "-m", "git push readme"], work)
    head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
    pack = _run_git(["pack-objects", "--stdout", "--revs"], work, f"{head}\n".encode("ascii"))
    command = (
        f"{'0' * 40} {head} refs/heads/main"
        "\0 report-status side-band-64k object-format=sha1\n"
    ).encode("ascii")
    return _pkt_line(command) + b"0000" + pack, head


def _git_access_auth(
    *,
    scope_id: str = "docs-scope",
    scope_path: str = "/docs/",
    mode: str = "rw",
    exclude: list[str] | None = None,
    user_identity: str = "",
) -> tuple[str, dict]:
    normalized_path = scope_path.strip("/")
    target = (
        ScopeTarget(project_id="test-proj", scope_id=scope_id)
        if normalized_path
        else ProjectRootTarget(project_id="test-proj")
    )
    normalized_excludes = tuple(item.strip("/") for item in (exclude or []))
    view = ResolvedRepositoryView(
        target=target,
        path_prefix=normalized_path,
        excludes=normalized_excludes,
        max_mode=(mode if normalized_path else "rw"),
    )
    return (
        "test-proj",
        {
            "agent": f"scope:{scope_id}",
            "_runtime_grant": RuntimeGrant(
                principal=RuntimePrincipal(
                    principal_id=scope_id,
                    credential_kind="test_access_key",
                ),
                target=target,
                repository_view=view,
                mode=RuntimeMode(mode),
            ),
            "_repo_facade": {"id": scope_id, "kind": "access_point"},
            "_project_id": "test-proj",
            "_user_identity": user_identity,
        },
    )


def _patch_git_scope_auth(monkeypatch, mapping: dict[str, tuple]):
    def fake_resolve(access_key: str):
        values = mapping[access_key]
        scope_id, scope_path, mode = values[:3]
        exclude = values[3] if len(values) > 3 else None
        return _git_access_auth(
            scope_id=scope_id,
            scope_path=scope_path,
            mode=mode,
            exclude=exclude,
        )

    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        fake_resolve,
    )


def _patch_canonical_git_credentials(
    monkeypatch,
    *,
    scope_id: str,
    scope_path: str,
    is_root: bool,
    token: str = "git_secret",
) -> None:
    from src.version_engine.entrypoints.git import auth as git_auth

    class _Credentials:
        def __init__(self, _client):
            pass

        def resolve_git_runtime_credential(self, raw_token: str):
            if raw_token != token:
                return None
            return {
                "credential_id": "credential-canonical",
                "project_id": "test-proj",
                "access_surface_id": "surface-git",
                "target_kind": "project_root" if is_root else "scope",
                "scope_id": None if is_root else scope_id,
                "path_prefix": "" if is_root else scope_path.strip("/"),
                "excludes": [],
                "target_max_mode": "rw",
                "user_id": None,
                "effective_mode": "rw",
            }

    monkeypatch.setattr(
        git_auth,
        "SupabaseClient",
        lambda: type("_Supabase", (), {"client": object()})(),
    )
    monkeypatch.setattr(git_auth, "AccessCredentialRepository", _Credentials)
    monkeypatch.setattr(git_auth.settings, "SKIP_AUTH", False)
    monkeypatch.setattr(git_auth, "enforce_channel_pause", lambda *_a, **_k: None)


def _receive_command_body(old_id: str, new_id: str, ref: str, pack: bytes = b"") -> bytes:
    command = (
        f"{old_id} {new_id} {ref}"
        "\0 report-status side-band-64k object-format=sha1\n"
    ).encode("ascii")
    return _pkt_line(command) + b"0000" + pack


class TestGitNativeSubmission:
    @pytest.mark.asyncio
    async def test_current_git_push_preserves_client_commit_object(
        self, repo_manager, server_repo,
    ):
        tree_id = build_tree_from_files(server_repo.store, {"README.md": b"hi"})
        client_commit_id = _make_client_commit(server_repo, tree_id)

        result = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:user",
            base_commit_id="",
            proposed_tree_id=tree_id,
            client_commit_id=client_commit_id,
            message="add readme",
        )

        assert result.commit_id == client_commit_id
        assert server_repo.get_scope_head_commit_id("") == client_commit_id
        assert _files_for_scope(server_repo) == {"README.md": b"hi"}
        assert server_repo.history._entries[-1]["commit_id"] == client_commit_id
        assert server_repo.audit.events[-1]["type"] == "access_git_push"

    @pytest.mark.asyncio
    async def test_stale_git_push_is_rejected_like_git_hosts(
        self, repo_manager, server_repo,
    ):
        first_tree = build_tree_from_files(server_repo.store, {"a.txt": b"server"})
        first_commit = _make_client_commit(server_repo, first_tree)
        first = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:user",
            base_commit_id="",
            proposed_tree_id=first_tree,
            client_commit_id=first_commit,
            message="add a",
        )

        stale_tree = build_tree_from_files(server_repo.store, {"b.txt": b"client"})
        stale_client_commit = _make_client_commit(server_repo, stale_tree)
        with pytest.raises(NonFastForwardSubmissionError):
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="",
                actor="git:user",
                base_commit_id="",
                proposed_tree_id=stale_tree,
                client_commit_id=stale_client_commit,
                message="stale add b",
            )

        assert first.commit_id == first_commit
        assert _files_for_scope(server_repo) == {"a.txt": b"server"}
        assert server_repo.get_scope_head_commit_id("") == first.commit_id

    @pytest.mark.asyncio
    async def test_git_push_touching_child_scope_is_rejected(
        self, repo_manager, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        tree_id = build_tree_from_files(server_repo.store, {"docs/a.md": b"hidden"})
        client_commit_id = _make_client_commit(server_repo, tree_id)

        with pytest.raises(CrossScopeSubmissionError) as exc:
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="",
                actor="git:user",
                base_commit_id="",
                proposed_tree_id=tree_id,
                client_commit_id=client_commit_id,
                message="bad root write",
            )

        assert exc.value.rejected_paths == ["docs/a.md"]
        assert server_repo.history._entries == []
        assert server_repo.get_scope_head_commit_id("") == ""

    @pytest.mark.asyncio
    async def test_git_push_touching_excluded_path_is_rejected(
        self, repo_manager, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/", exclude=["/docs/secret/"])
        tree_id = build_tree_from_files(
            server_repo.store,
            {"secret/classified.md": b"nope"},
        )
        client_commit_id = _make_client_commit(server_repo, tree_id)

        with pytest.raises(CrossScopeSubmissionError) as exc:
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="docs",
                scope_excludes=["/docs/secret/"],
                actor="git:user",
                base_commit_id="",
                proposed_tree_id=tree_id,
                client_commit_id=client_commit_id,
                message="bad excluded write",
            )

        assert exc.value.rejected_paths == ["docs/secret/classified.md"]
        assert server_repo.history._entries == []
        assert server_repo.get_scope_head_commit_id("docs") == ""

    @pytest.mark.asyncio
    async def test_stale_git_json_push_is_rejected(self, repo_manager, server_repo):
        base_tree = build_tree_from_files(
            server_repo.store,
            {"config.json": b'{"a": 1, "b": 1}\n'},
        )
        base_commit = _make_client_commit(server_repo, base_tree, message="base config")
        base = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:a",
            base_commit_id="",
            proposed_tree_id=base_tree,
            client_commit_id=base_commit,
            message="base config",
        )

        current_tree = build_tree_from_files(
            server_repo.store,
            {"config.json": b'{"a": 2, "b": 1}\n'},
        )
        current_commit = _make_client_commit(
            server_repo, current_tree, parent_id=base.commit_id, message="change a",
        )
        await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:a",
            base_commit_id=base.commit_id,
            proposed_tree_id=current_tree,
            client_commit_id=current_commit,
            message="change a",
        )

        stale_tree = build_tree_from_files(
            server_repo.store,
            {"config.json": b'{"a": 1, "b": 3}\n'},
        )
        stale_commit = _make_client_commit(
            server_repo, stale_tree, parent_id=base.commit_id, message="change b",
        )
        with pytest.raises(NonFastForwardSubmissionError):
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="",
                actor="git:b",
                base_commit_id=base.commit_id,
                proposed_tree_id=stale_tree,
                client_commit_id=stale_commit,
                message="change b",
            )

        current_config = json.loads(_files_for_scope(server_repo)["config.json"])
        assert current_config == {"a": 2, "b": 1}
        assert server_repo.get_scope_head_commit_id("") == current_commit

    @pytest.mark.asyncio
    async def test_concurrent_git_pushes_same_scope_have_single_winner(
        self, repo_manager, server_repo,
    ):
        async def push(name: str):
            tree_id = build_tree_from_files(server_repo.store, {f"{name}.txt": name.encode()})
            commit_id = _make_client_commit(server_repo, tree_id, message=f"add {name}")
            return await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="",
                actor=f"git:{name}",
                base_commit_id="",
                proposed_tree_id=tree_id,
                client_commit_id=commit_id,
                message=f"add {name}",
            )

        results = await asyncio.gather(push("a"), push("b"), push("c"), return_exceptions=True)
        winners = [result for result in results if not isinstance(result, Exception)]
        rejected = [
            result
            for result in results
            if isinstance(result, NonFastForwardSubmissionError)
        ]

        assert len(winners) == 1
        assert len(rejected) == 2
        assert len(_files_for_scope(server_repo)) == 1
        assert server_repo.get_scope_head_commit_id("") == winners[0].commit_id

    @pytest.mark.asyncio
    async def test_git_push_rechecks_source_scope_head_at_publish(
        self, repo_manager, server_repo,
    ):
        scope_path = "docs"
        server_repo.add_scope("docs-scope", f"/{scope_path}/")
        base_scope_tree = build_tree_from_files(
            server_repo.store,
            {"readme.md": b"base\n"},
        )
        base_scope_commit = _make_client_commit(
            server_repo,
            base_scope_tree,
            message="base scope",
        )
        base_root_tree = build_tree_from_files(
            server_repo.store,
            {f"{scope_path}/readme.md": b"base\n"},
        )
        base_root_commit = _make_client_commit(
            server_repo,
            base_root_tree,
            message="base root",
        )
        server_repo.history.set_root_hash(base_root_tree)
        server_repo.history.set_scope_hash("", base_root_tree)
        server_repo.set_scope_head_commit_id("", base_root_commit)
        server_repo.history.set_scope_hash(scope_path, base_scope_tree)
        server_repo.set_scope_head_commit_id(scope_path, base_scope_commit)

        incoming_tree = build_tree_from_files(
            server_repo.store,
            {"readme.md": b"client update\n"},
        )
        incoming_commit = _make_client_commit(
            server_repo,
            incoming_tree,
            parent_id=base_scope_commit,
            message="client update",
        )
        moved_scope_head = _make_client_commit(
            server_repo,
            base_scope_tree,
            parent_id=base_scope_commit,
            message="scope view moved",
        )

        original_publish = server_repo.publish_project_update
        raced = False

        def publish_after_scope_head_moves(**kwargs):
            nonlocal raced
            if not raced:
                raced = True
                server_repo.set_scope_head_commit_id(scope_path, moved_scope_head)
            return original_publish(**kwargs)

        server_repo.publish_project_update = publish_after_scope_head_moves

        with pytest.raises(NonFastForwardSubmissionError):
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path=scope_path,
                actor="git:bob",
                base_commit_id=base_scope_commit,
                proposed_tree_id=incoming_tree,
                client_commit_id=incoming_commit,
                message="client update",
            )

        assert raced
        assert server_repo.get_root_hash() == base_root_tree
        assert server_repo.get_scope_head_commit_id(scope_path) == moved_scope_head
        assert _files_for_scope(server_repo, scope_path) == {"readme.md": b"base\n"}

    @pytest.mark.asyncio
    async def test_server_merge_publishes_scope_view_commit_not_unmerged_client_sha(
        self, repo_manager, server_repo,
    ):
        scope_path = "docs"
        server_repo.add_scope("docs-scope", f"/{scope_path}/")
        base_tree = build_tree_from_files(
            server_repo.store,
            {"index.md": b"v0\n"},
        )
        base_commit = _make_client_commit(
            server_repo,
            base_tree,
            message="base docs",
        )
        base_root = build_tree_from_files(
            server_repo.store,
            {f"{scope_path}/index.md": b"v0\n"},
        )
        root_commit = _make_client_commit(
            server_repo,
            base_root,
            message="base root",
        )
        server_repo.history.set_root_hash(base_root)
        server_repo.history.set_scope_hash("", base_root)
        server_repo.set_scope_head_commit_id("", root_commit)
        server_repo.history.set_scope_hash(scope_path, base_tree)
        server_repo.set_scope_head_commit_id(scope_path, base_commit)

        engine = VersionWriteEngine(repo_manager)
        alice_tree = build_tree_from_files(
            server_repo.store,
            {"index.md": b"v0\n", "alice.md": b"A\n"},
        )
        alice_commit = _make_client_commit(
            server_repo,
            alice_tree,
            parent_id=base_commit,
            message="alice",
        )
        await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj",
            scope_path=scope_path,
            actor="papi:alice",
            source_channel="papi",
            base_commit_id=base_commit,
            proposed_tree_id=alice_tree,
            client_commit_id=alice_commit,
            message="alice",
        ))

        bob_tree = build_tree_from_files(
            server_repo.store,
            {"index.md": b"v0\n", "bob.md": b"B\n"},
        )
        bob_commit = _make_client_commit(
            server_repo,
            bob_tree,
            parent_id=base_commit,
            message="bob from stale base",
        )
        result = await engine.submit_version(VersionSubmissionIntent(
            project_id="test-proj",
            scope_path=scope_path,
            actor="papi:bob",
            source_channel="papi",
            base_commit_id=base_commit,
            proposed_tree_id=bob_tree,
            client_commit_id=bob_commit,
            message="bob from stale base",
        ))

        assert result.status == "ok"
        final_scope_hash = server_repo.get_scope_hash(scope_path)
        final_scope_head = server_repo.get_scope_head_commit_id(scope_path)
        assert final_scope_head != bob_commit
        assert commit_tree_id(server_repo, final_scope_head) == final_scope_hash
        assert _files_for_scope(server_repo, scope_path) == {
            "index.md": b"v0\n",
            "alice.md": b"A\n",
            "bob.md": b"B\n",
        }

    @pytest.mark.asyncio
    async def test_same_scope_operations_compute_in_parallel_and_cas_retry(
        self, repo_manager, server_repo,
    ):
        engine = VersionWriteEngine(repo_manager)
        barrier = threading.Barrier(2, timeout=2)
        calls: dict[str, int] = {"a": 0, "b": 0}

        async def write(name: str):
            def splice(store, root_hash):
                calls[name] += 1
                if calls[name] == 1:
                    barrier.wait()
                return splice_put_blob(
                    store,
                    root_hash,
                    f"{name}.txt",
                    name.encode(),
                )

            return await engine.apply_operation(
                OperationWriteIntent(
                    project_id="test-proj",
                    scope_path="",
                    actor=f"papi:{name}",
                    source_channel="papi",
                    operation_type="write_file",
                    message=f"write {name}",
                    audit_detail={"path": f"{name}.txt"},
                ),
                splice,
            )

        results = await asyncio.gather(write("a"), write("b"))

        assert len({result.commit_id for result in results}) == 2
        assert _files_for_scope(server_repo) == {
            "a.txt": b"a",
            "b.txt": b"b",
        }
        assert sum(calls.values()) == 3
        assert sorted(calls.values()) == [1, 2]
        assert any(
            event["detail"]["cas_attempts"] == 2
            for event in server_repo.audit.events
        )

    @pytest.mark.asyncio
    async def test_git_pushes_to_different_scopes_advance_independent_heads(
        self, repo_manager, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        server_repo.add_scope("src-scope", "/src/")
        docs_tree = build_tree_from_files(server_repo.store, {"README.md": b"docs"})
        src_tree = build_tree_from_files(server_repo.store, {"app.py": b"print('src')"})
        docs_commit = _make_client_commit(server_repo, docs_tree, message="docs")
        src_commit = _make_client_commit(server_repo, src_tree, message="src")

        await asyncio.gather(
            submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="docs",
                actor="git:docs",
                base_commit_id="",
                proposed_tree_id=docs_tree,
                client_commit_id=docs_commit,
                message="docs",
            ),
            submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="src",
                actor="git:src",
                base_commit_id="",
                proposed_tree_id=src_tree,
                client_commit_id=src_commit,
                message="src",
            ),
        )

        assert server_repo.get_scope_head_commit_id("docs") == docs_commit
        assert server_repo.get_scope_head_commit_id("src") == src_commit
        assert _files_for_scope(server_repo, "docs") == {"README.md": b"docs"}
        assert _files_for_scope(server_repo, "src") == {"app.py": b"print('src')"}

    @pytest.mark.asyncio
    async def test_stale_git_delete_modify_is_rejected(
        self, repo_manager, server_repo,
    ):
        """Git remotes reject stale delete-vs-modify pushes.

        LWW and manual review remain product/review semantics; the Git remote
        contract is fetch/rebase before push, matching normal Git hosts.
        """
        base_tree = build_tree_from_files(server_repo.store, {"a.txt": b"base\n"})
        base_commit = _make_client_commit(server_repo, base_tree, message="base")
        base = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:base",
            base_commit_id="",
            proposed_tree_id=base_tree,
            client_commit_id=base_commit,
            message="base",
        )

        server_tree = build_tree_from_files(server_repo.store, {"a.txt": b"server changed\n"})
        server_commit = _make_client_commit(
            server_repo, server_tree, parent_id=base.commit_id, message="server modifies",
        )
        await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:server",
            base_commit_id=base.commit_id,
            proposed_tree_id=server_tree,
            client_commit_id=server_commit,
            message="server modifies",
        )
        prior_head = server_repo.get_scope_head_commit_id("")

        delete_tree = build_tree_from_files(server_repo.store, {})
        delete_commit = _make_client_commit(
            server_repo, delete_tree, parent_id=base.commit_id, message="stale deletes",
        )
        with pytest.raises(NonFastForwardSubmissionError):
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="",
                actor="git:stale",
                base_commit_id=base.commit_id,
                proposed_tree_id=delete_tree,
                client_commit_id=delete_commit,
                message="stale deletes",
            )

        assert _files_for_scope(server_repo) == {"a.txt": b"server changed\n"}
        assert server_repo.get_scope_head_commit_id("") == prior_head

    @pytest.mark.asyncio
    async def test_stale_git_binary_conflict_is_rejected(
        self, repo_manager, server_repo,
    ):
        """Git remotes reject stale binary conflicts before product policy."""
        base_tree = build_tree_from_files(server_repo.store, {"asset.bin": b"\x00base"})
        base_commit = _make_client_commit(server_repo, base_tree, message="base binary")
        base = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:base",
            base_commit_id="",
            proposed_tree_id=base_tree,
            client_commit_id=base_commit,
            message="base binary",
        )

        server_tree = build_tree_from_files(server_repo.store, {"asset.bin": b"\x00server"})
        server_commit = _make_client_commit(
            server_repo, server_tree, parent_id=base.commit_id, message="server binary",
        )
        await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:server",
            base_commit_id=base.commit_id,
            proposed_tree_id=server_tree,
            client_commit_id=server_commit,
            message="server binary",
        )
        prior_head = server_repo.get_scope_head_commit_id("")

        stale_tree = build_tree_from_files(server_repo.store, {"asset.bin": b"\x00client"})
        stale_commit = _make_client_commit(
            server_repo, stale_tree, parent_id=base.commit_id, message="client binary",
        )
        with pytest.raises(NonFastForwardSubmissionError):
            await submit_git_tree(
                repo_manager,
                project_id="test-proj",
                scope_path="",
                actor="git:client",
                base_commit_id=base.commit_id,
                proposed_tree_id=stale_tree,
                client_commit_id=stale_commit,
                message="client binary",
            )

        assert _files_for_scope(server_repo) == {"asset.bin": b"\x00server"}
        assert server_repo.get_scope_head_commit_id("") == prior_head


class TestVersionSubmissionAdapter:
    @pytest.mark.asyncio
    async def test_agent_push_uses_same_git_native_engine(self, repo_manager, server_repo):
        tree_id = build_tree_from_files(server_repo.store, {"main.py": b"print(1)"})
        reachable = tree_mod.collect_reachable_hashes(server_repo.store, tree_id)
        objects_b64 = {
            object_id: base64.b64encode(server_repo.store.get_loose(object_id)).decode()
            for object_id in reachable
        }
        auth = _git_access_auth(scope_id="root", scope_path="")[1]
        auth["agent"] = "version-agent"

        result = await submit_agent_push(
            repo_manager,
            "test-proj",
            auth,
            {
                "protocol_version": PROTOCOL_VERSION,
                "base_commit_id": "",
                "snapshots": [{
                    "id": 1,
                    "root": tree_id,
                    "message": "version submission",
                    "who": "version-agent",
                    "time": "2026-01-01T00:00:00Z",
                }],
                "objects": objects_b64,
            },
        )

        assert result["status"] == "ok"
        assert len(result["commit_id"]) == 40
        assert result["root"] == server_repo.get_scope_hash("")
        assert _files_for_scope(server_repo) == {"main.py": b"print(1)"}
        assert server_repo.audit.events[-1]["type"] == "agent_push"

    @pytest.mark.asyncio
    async def test_agent_push_touching_excluded_path_uses_same_scope_guard(
        self, repo_manager, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/", exclude=["/docs/secret/"])
        tree_id = build_tree_from_files(
            server_repo.store,
            {"secret/classified.md": b"hidden"},
        )
        reachable = tree_mod.collect_reachable_hashes(server_repo.store, tree_id)
        objects_b64 = {
            object_id: base64.b64encode(server_repo.store.get_loose(object_id)).decode()
            for object_id in reachable
        }
        auth = _git_access_auth(
            scope_id="docs-scope",
            scope_path="/docs/",
            exclude=["/docs/secret/"],
        )[1]
        auth["agent"] = "version-agent"

        with pytest.raises(PermissionError, match="outside its scope"):
            await submit_agent_push(
                repo_manager,
                "test-proj",
                auth,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "base_commit_id": "",
                    "snapshots": [{
                        "id": 1,
                        "root": tree_id,
                        "message": "version excluded push",
                        "who": "version-agent",
                        "time": "2026-01-01T00:00:00Z",
                    }],
                    "objects": objects_b64,
                },
            )

        assert server_repo.get_scope_head_commit_id("docs") == ""


class TestGitNativeHardeningContracts:
    @pytest.mark.asyncio
    async def test_engine_publishes_through_single_repo_boundary(
        self, repo_manager, server_repo,
    ):
        calls = []

        def fake_publish(**kwargs):
            calls.append(kwargs)
            assert server_repo.history.cas_update_root_hash(
                kwargs["old_root_hash"],
                kwargs["new_root_hash"],
            )
            scope_path = (kwargs.get("scope_path") or "").strip("/")
            scope_hash = kwargs.get("scope_hash") or kwargs["new_root_hash"]
            server_repo.history.set_scope_hash("", kwargs["new_root_hash"])
            server_repo.history.set_scope_head_commit_id("", kwargs["commit_id"])
            server_repo.history.set_scope_hash(scope_path, scope_hash)
            server_repo.history.set_scope_head_commit_id(scope_path, kwargs["commit_id"])
            server_repo.history.record(
                kwargs["commit_id"],
                kwargs["who"],
                kwargs["message"],
                scope_path,
                kwargs["changes"],
                kwargs["conflicts"],
                root_hash=kwargs["new_root_hash"],
                scope_hash=scope_hash,
                created_at_iso=kwargs["created_at_iso"],
            )
            server_repo.audit.record(
                kwargs["audit_event_type"],
                kwargs["audit_agent_id"],
                kwargs["audit_detail"],
            )
            return True, None

        server_repo.publish_project_update = fake_publish
        server_repo.record_history = MagicMock(side_effect=AssertionError("bypassed publish"))
        server_repo.record_audit = MagicMock(side_effect=AssertionError("bypassed publish"))

        tree_id = build_tree_from_files(server_repo.store, {"atomic.txt": b"ok"})
        commit_id = _make_client_commit(server_repo, tree_id, message="atomic")
        result = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:atomic",
            base_commit_id="",
            proposed_tree_id=tree_id,
            client_commit_id=commit_id,
            message="atomic",
        )

        assert result.status == "ok"
        assert len(calls) == 1
        assert calls[0]["audit_event_type"] == "access_git_push"
        assert calls[0]["commit_id"] == commit_id
        assert calls[0]["new_root_hash"] == tree_id
        assert calls[0]["scope_hash"] == tree_id

    @pytest.mark.asyncio
    async def test_project_operation_publish_does_not_wait_for_sync_projection(
        self, repo_manager, server_repo, monkeypatch,
    ):
        from src.version_engine.derived import hooks

        save_latency_budget_ms = 500
        scheduled: dict[str, dict] = {}

        def forbidden_barrier(*args, **kwargs):
            raise AssertionError("project save waited for visibility barrier")

        def fake_schedule(project_id, manager, push_result):
            scheduled["project_id"] = project_id
            scheduled["push_result"] = push_result

        monkeypatch.setattr(
            hooks, "run_project_root_visibility_barrier", forbidden_barrier,
        )
        monkeypatch.setattr(
            hooks, "schedule_post_project_update_hook", fake_schedule,
        )

        started = time.perf_counter()
        result = await VersionWriteEngine(repo_manager).apply_project_operation(
            OperationWriteIntent(
                project_id="test-proj",
                scope_path="",
                actor="user:save",
                source_channel="web",
                operation_type="write_file",
                message="fast project save",
                audit_detail={"path": "fast.md"},
            ),
            lambda store, root: splice_put_blob(store, root, "fast.md", b"ok"),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert result.status == "ok"
        assert elapsed_ms < save_latency_budget_ms
        assert scheduled["project_id"] == "test-proj"
        assert scheduled["push_result"]["commit_id"] == result.commit_id
        audit = _last_audit_event(server_repo, "write_file")
        assert audit["detail"]["source_channel"] == "web"

    @pytest.mark.asyncio
    async def test_version_rollback_uses_engine_and_updates_grafted_history(
        self, repo_manager, server_repo,
    ):
        v1_tree = build_tree_from_files(server_repo.store, {"doc.md": b"v1"})
        v1_commit = _make_client_commit(server_repo, v1_tree, message="v1")
        v1 = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:user",
            base_commit_id="",
            proposed_tree_id=v1_tree,
            client_commit_id=v1_commit,
            message="v1",
        )
        v2_tree = build_tree_from_files(server_repo.store, {"doc.md": b"v2"})
        v2_commit = _make_client_commit(server_repo, v2_tree, parent_id=v1.commit_id, message="v2")
        await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:user",
            base_commit_id=v1.commit_id,
            proposed_tree_id=v2_tree,
            client_commit_id=v2_commit,
            message="v2",
        )

        result = await submit_version_rollback(
            repo_manager,
            "test-proj",
            {
                **_git_access_auth(scope_id="root", scope_path="")[1],
                "agent": "version-agent",
            },
            {"protocol_version": PROTOCOL_VERSION, "target_commit_id": v1.commit_id},
        )

        assert result["status"] == "rolled-back"
        assert result["new_commit_id"] != v1.commit_id
        assert _files_for_scope(server_repo) == {"doc.md": b"v1"}
        assert server_repo.audit.events[-1]["type"] == "rollback"
        assert server_repo.history._version_index

    @pytest.mark.asyncio
    async def test_project_view_history_uses_persistent_graft_index(
        self, repo_manager, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        tree_id = build_tree_from_files(server_repo.store, {"README.md": b"docs"})
        client_commit = _make_client_commit(server_repo, tree_id, message="docs")

        result = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="docs",
            actor="git:docs",
            base_commit_id="",
            proposed_tree_id=tree_id,
            client_commit_id=client_commit,
            message="docs",
        )

        from src.version_engine.derived.hooks import run_post_project_update_hook

        run_post_project_update_hook(
            "test-proj",
            repo_manager,
            {
                "status": "ok",
                "commit_id": result.commit_id,
                "root": server_repo.get_root_hash(),
                "scope_path": "docs",
                "scope_hash": result.new_scope_hash,
            },
        )

        indexed = server_repo.history._version_index[-1]
        project_head = git_view_head_commit(server_repo, "")
        assert project_head == indexed["project_view_commit_id"]
        assert commit_tree_id(server_repo, project_head) == indexed["project_root_hash"]

    def test_empty_project_shell_has_no_git_head(self, server_repo):
        empty_tree = _init_empty_project_shell(server_repo)

        assert server_repo.get_root_hash() == empty_tree
        assert server_repo.get_head_commit_id() == ""
        assert git_view_head_commit(server_repo, "") == ""

    def test_deleted_scope_path_does_not_resurrect_stale_scope_cache(
        self,
        server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        stale_tree = build_tree_from_files(
            server_repo.store,
            {"old.md": b"stale cache\n"},
        )
        stale_commit = _make_client_commit(
            server_repo,
            stale_tree,
            message="stale docs",
        )
        root_tree = build_tree_from_files(
            server_repo.store,
            {"README.md": b"root still exists\n"},
        )
        root_commit = _make_client_commit(
            server_repo,
            root_tree,
            message="root without docs",
        )
        server_repo.history.set_root_hash(root_tree)
        server_repo.history.set_scope_hash("", root_tree)
        server_repo.set_scope_head_commit_id("", root_commit)
        server_repo.history.set_scope_hash("docs", stale_tree)
        server_repo.set_scope_head_commit_id("docs", stale_commit)

        assert git_view_head_commit(server_repo, "docs") == ""

    def test_build_git_commit_rejects_non_git_parent(
        self, server_repo,
    ):
        tree_id = build_tree_from_files(server_repo.store, {"README.md": b"hello\n"})

        with pytest.raises(GitCommitInvariantError):
            build_git_commit(
                server_repo,
                tree_sha=tree_id,
                parent_sha="1dda56a9166ce3d1",
                who="web:user",
                message="bad parent",
                created_at_iso="2026-01-01T00:00:00+00:00",
            )

    def test_quarantine_materializes_reachable_objects_without_full_store(
        self, tmp_path, server_repo,
    ):
        reachable_tree = build_tree_from_files(server_repo.store, {"a.txt": b"a"})
        reachable_commit = _make_client_commit(server_repo, reachable_tree, message="reachable")
        unreachable_blob = server_repo.store.put_blob(b"unreachable")
        bare_dir = tmp_path / "repo.git"
        run_git(["init", "--bare", str(bare_dir)])

        copy_reachable_objects_to_bare(server_repo, bare_dir, [reachable_commit])

        assert (bare_dir / "objects" / reachable_commit[:2] / reachable_commit[2:]).exists()
        assert (bare_dir / "objects" / reachable_tree[:2] / reachable_tree[2:]).exists()
        assert not (bare_dir / "objects" / unreachable_blob[:2] / unreachable_blob[2:]).exists()

    def test_transport_cache_does_not_decode_blob_bodies_while_walking(
        self, tmp_path, server_repo, monkeypatch,
    ):
        reachable_tree = build_tree_from_files(
            server_repo.store,
            {"large.bin": b"x" * (2 * 1024 * 1024)},
        )
        blob_id = tree_mod.read_tree(server_repo.store, reachable_tree)["large.bin"][1]
        reachable_commit = _make_client_commit(server_repo, reachable_tree, message="reachable")
        bare_dir = tmp_path / "repo.git"
        run_git(["init", "--bare", str(bare_dir)])

        original_get_object = server_repo.store.get_object

        def tracked_get_object(object_id):
            if object_id == blob_id:
                raise AssertionError("transport cache should fetch blob loose bytes directly")
            return original_get_object(object_id)

        monkeypatch.setattr(server_repo.store, "get_object", tracked_get_object)

        copy_reachable_objects_to_bare(server_repo, bare_dir, [reachable_commit])

        assert (bare_dir / "objects" / blob_id[:2] / blob_id[2:]).exists()

    def test_receive_transport_cache_stops_at_current_head_boundary(
        self, tmp_path, server_repo,
    ):
        base_tree = build_tree_from_files(server_repo.store, {"a.txt": b"base"})
        base_commit = _make_client_commit(server_repo, base_tree, message="base")
        head_tree = build_tree_from_files(server_repo.store, {"a.txt": b"head"})
        head_commit = _make_client_commit(
            server_repo,
            head_tree,
            parent_id=base_commit,
            message="head",
        )
        bare_dir = tmp_path / "repo.git"
        run_git(["init", "--bare", str(bare_dir)])

        copy_reachable_objects_to_bare(
            server_repo,
            bare_dir,
            [head_commit],
            follow_history=False,
        )

        assert (bare_dir / "objects" / head_commit[:2] / head_commit[2:]).exists()
        assert not (bare_dir / "objects" / base_commit[:2] / base_commit[2:]).exists()

    def test_git_view_cache_uses_configured_durable_root_and_metadata(
        self, tmp_path, server_repo, monkeypatch,
    ):
        cache_root = tmp_path / "git-view-cache"
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_DIR", cache_root, raising=False)
        server_repo.add_scope("docs-scope", "/docs/")
        tree_id = build_tree_from_files(server_repo.store, {"README.md": b"cached\n"})
        head_commit = _make_client_commit(server_repo, tree_id, message="head")
        server_repo.set_scope_head_commit_id("docs", head_commit)

        with transport_bare_repo(server_repo, "docs") as bare_dir:
            cache_dir = bare_dir.parent
            metadata = json.loads((cache_dir / "view.json").read_text(encoding="utf-8"))

        assert cache_root.resolve() in cache_dir.resolve().parents
        assert metadata["project_id"] == "test-proj"
        assert metadata["scope_path"] == "docs"
        assert metadata["scope_excludes"] == []
        assert metadata["history_mode"] == "full"
        assert metadata["blob_mode"] == "included"
        assert metadata["cache_head"] == head_commit

        # The cache is disposable: deleting it must rebuild from canonical
        # object storage on the next transport request with identical content.
        shutil.rmtree(cache_dir)
        assert not cache_dir.exists()
        with transport_bare_repo(server_repo, "docs") as rebuilt_bare:
            rebuilt_metadata = json.loads(
                (rebuilt_bare.parent / "view.json").read_text(encoding="utf-8")
            )
            assert rebuilt_metadata["cache_head"] == head_commit
            assert (rebuilt_bare / "objects" / head_commit[:2] / head_commit[2:]).exists()

    def test_receive_quarantine_uses_blob_included_boundary_cache_by_default(
        self, tmp_path, server_repo, monkeypatch,
    ):
        cache_root = tmp_path / "git-view-cache"
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_DIR", cache_root, raising=False)
        server_repo.add_scope("docs-scope", "/docs/")
        tree_id = build_tree_from_files(server_repo.store, {"notes.md": b"base blob\n"})
        blob_id = tree_mod.read_tree(server_repo.store, tree_id)["notes.md"][1]
        head_commit = _make_client_commit(server_repo, tree_id, message="head")
        server_repo.set_scope_head_commit_id("docs", head_commit)

        with quarantine_bare_repo(server_repo, "docs") as bare_dir:
            alternate_objects = Path(
                (bare_dir / "objects" / "info" / "alternates")
                .read_text(encoding="utf-8")
                .strip()
            )
            cache_dir = alternate_objects.parent.parent
            metadata = json.loads((cache_dir / "view.json").read_text(encoding="utf-8"))

        assert metadata["history_mode"] == "receive-boundary"
        assert metadata["blob_mode"] == "included"
        assert (alternate_objects / blob_id[:2] / blob_id[2:]).exists()

    def test_receive_pack_advertisement_is_refs_only(
        self, server_repo, monkeypatch,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        head_tree = build_tree_from_files(
            server_repo.store,
            {"large.bin": b"x" * (2 * 1024 * 1024)},
        )
        head_commit = _make_client_commit(server_repo, head_tree, message="head")
        server_repo.set_scope_head_commit_id("docs", head_commit)
        blob_id = tree_mod.read_tree(server_repo.store, head_tree)["large.bin"][1]

        original_get_object = server_repo.store.get_object

        def fail_get_object(object_id):
            if object_id == blob_id:
                raise AssertionError(f"receive advertisement hydrated blob {object_id}")
            return original_get_object(object_id)

        monkeypatch.setattr(server_repo.store, "get_object", fail_get_object)

        with receive_pack_advertisement_bare_repo(server_repo, "docs") as bare_dir:
            advertised = run_git([
                "receive-pack",
                "--stateless-rpc",
                "--advertise-refs",
                str(bare_dir),
            ])

        assert head_commit.encode("ascii") in advertised
        assert b"refs/heads/main" in advertised

    def test_receive_pack_advertisement_and_quarantine_use_projected_head(
        self, tmp_path, server_repo, monkeypatch,
    ):
        cache_root = tmp_path / "git-view-cache"
        monkeypatch.setattr(settings, "GIT_VIEW_CACHE_DIR", cache_root, raising=False)
        server_repo.add_scope("docs-scope", "/docs/")
        malformed_tree = server_repo.store.put_tree(
            b"40000 broken\x00" + bytes.fromhex("28a44dbeee08f49e"),
        )
        bad_commit = _make_client_commit(
            server_repo,
            malformed_tree,
            message="legacy malformed tree",
        )
        current_tree = build_tree_from_files(server_repo.store, {"README.md": b"current\n"})
        current_commit = _make_client_commit(
            server_repo,
            current_tree,
            parent_id=bad_commit,
            message="current after legacy damage",
        )
        server_repo.history.set_scope_hash("docs", current_tree)
        server_repo.set_scope_head_commit_id("docs", current_commit)
        projected = git_view_head_commit(server_repo, "docs")

        assert projected != current_commit
        with receive_pack_advertisement_bare_repo(server_repo, "docs") as bare_dir:
            advertised = run_git([
                "receive-pack",
                "--stateless-rpc",
                "--advertise-refs",
                str(bare_dir),
            ])

        assert projected.encode("ascii") in advertised
        assert current_commit.encode("ascii") not in advertised
        with quarantine_bare_repo(server_repo, "docs") as bare_dir:
            advertised_ref = (
                bare_dir / "refs" / "heads" / "main"
            ).read_text(encoding="ascii").strip()

        assert advertised_ref == projected

    def test_git_view_normalizes_invalid_parent_before_upload_pack(
        self, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        bad_tree = build_tree_from_files(server_repo.store, {"README.md": b"invalid-parent\n"})
        bad_commit = _make_client_commit(
            server_repo,
            bad_tree,
            parent_id="1dda56a9166ce3d1",
            message="invalid parent fixture",
        )
        head_tree = build_tree_from_files(server_repo.store, {"README.md": b"current\n"})
        head_commit = _make_client_commit(
            server_repo,
            head_tree,
            parent_id=bad_commit,
            message="current",
        )
        server_repo.history.set_scope_hash("docs", head_tree)
        server_repo.set_scope_head_commit_id("docs", head_commit)

        projected = git_view_head_commit(server_repo, "docs")

        assert projected != head_commit
        assert commit_tree_id(server_repo, projected) == head_tree
        with transport_bare_repo(server_repo, "docs") as bare_dir:
            run_git(["--git-dir", str(bare_dir), "fsck", "--full", "--strict"])

    def test_git_view_cuts_history_before_malformed_tree(
        self, tmp_path, server_repo, monkeypatch,
    ):
        monkeypatch.setattr(
            settings,
            "GIT_VIEW_CACHE_DIR",
            tmp_path / "git-view-cache",
            raising=False,
        )
        server_repo.add_scope("docs-scope", "/docs/")
        malformed_tree = server_repo.store.put_tree(
            b"40000 broken\x00" + bytes.fromhex("28a44dbeee08f49e"),
        )
        bad_commit = _make_client_commit(
            server_repo,
            malformed_tree,
            message="legacy malformed tree",
        )
        current_tree = build_tree_from_files(server_repo.store, {"README.md": b"current\n"})
        current_commit = _make_client_commit(
            server_repo,
            current_tree,
            parent_id=bad_commit,
            message="current after legacy damage",
        )
        server_repo.history.set_scope_hash("docs", current_tree)
        server_repo.set_scope_head_commit_id("docs", current_commit)

        projected = git_view_head_commit(server_repo, "docs")

        assert projected != current_commit
        assert commit_tree_id(server_repo, projected) == current_tree
        _obj_type, projected_body = server_repo.store.get_object(projected)
        projected_commit = decode_commit(projected_body)
        assert projected_commit["parents"] == []
        with transport_bare_repo(server_repo, "docs") as bare_dir:
            run_git(["--git-dir", str(bare_dir), "fsck", "--full", "--strict"])

    @pytest.mark.asyncio
    async def test_operation_after_invalid_head_uses_git_compatible_parent(
        self, repo_manager, server_repo,
    ):
        server_repo.add_scope("docs-scope", "/docs/")
        bad_tree = build_tree_from_files(server_repo.store, {"README.md": b"invalid-parent\n"})
        bad_commit = _make_client_commit(
            server_repo,
            bad_tree,
            parent_id="1dda56a9166ce3d1",
            message="invalid parent fixture",
        )
        server_repo.history.set_scope_hash("docs", bad_tree)
        server_repo.set_scope_head_commit_id("docs", bad_commit)

        result = await VersionWriteEngine(repo_manager).apply_operation(
            OperationWriteIntent(
                project_id="test-proj",
                scope_path="docs",
                actor="web:user",
                source_channel="web",
                operation_type="write_file",
                message="write after invalid head",
            ),
            lambda store, root: splice_put_blob(
                store,
                root,
                "next.md",
                b"new write\n",
            ),
        )

        assert result.commit_id != bad_commit
        assert is_git_compatible_commit(server_repo, result.commit_id)
        with transport_bare_repo(server_repo, "docs") as bare_dir:
            run_git(["--git-dir", str(bare_dir), "fsck", "--full", "--strict"])

    @pytest.mark.asyncio
    async def test_submit_version_does_not_preserve_malformed_client_commit(
        self, repo_manager, server_repo,
    ):
        tree_id = build_tree_from_files(server_repo.store, {"README.md": b"from client\n"})
        malformed_client_commit = _make_client_commit(
            server_repo,
            tree_id,
            parent_id="1dda56a9166ce3d1",
            message="malformed client commit",
        )

        result = await submit_git_tree(
            repo_manager,
            project_id="test-proj",
            scope_path="",
            actor="git:user",
            base_commit_id="",
            proposed_tree_id=tree_id,
            client_commit_id=malformed_client_commit,
            message="git push",
        )

        assert result.commit_id != malformed_client_commit
        assert is_git_compatible_commit(server_repo, result.commit_id)


def test_git_protocol_routes_exist():
    paths = {route.path for route in git_router.routes}
    assert "/git/{project_id}.git/info/refs" in paths
    assert "/git/{project_id}/scopes/{scope_id}.git/info/refs" in paths
    assert "/git/ap/{access_key}.git/info/refs" in paths
    assert "/git/{project_id}.git/git-receive-pack" in paths
    assert "/git/{project_id}/scopes/{scope_id}.git/git-receive-pack" in paths
    assert "/git/ap/{access_key}.git/git-receive-pack" in paths
    assert "/git/{project_id}.git/git-upload-pack" in paths
    assert "/git/{project_id}/scopes/{scope_id}.git/git-upload-pack" in paths
    assert "/git/ap/{access_key}.git/git-upload-pack" in paths


class _RpcResponse:
    def __init__(self, data):
        self.data = data


class _RpcCall:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeOutboxClient:
    def __init__(self, rows):
        self.rows = rows
        self.completed: list[int] = []
        self.failed: list[tuple[int, str]] = []

    def rpc(self, name, args):
        if name == CLAIM_OUTBOX_RPC:
            return _RpcCall(_RpcResponse(self.rows))
        if name == COMPLETE_OUTBOX_RPC:
            self.completed.append(args["p_id"])
            return _RpcCall(_RpcResponse(True))
        if name == FAIL_OUTBOX_RPC:
            self.failed.append((args["p_id"], args["p_error"]))
            return _RpcCall(_RpcResponse(True))
        raise AssertionError(name)


def test_version_outbox_worker_replays_hook_and_marks_complete(monkeypatch):
    calls = []

    def fake_hook(project_id, repo_manager, push_result, *, raise_errors=False):
        calls.append((project_id, repo_manager, push_result, raise_errors))

    monkeypatch.setattr(
        "src.version_engine.derived.outbox.run_post_push_hook",
        fake_hook,
    )
    client = _FakeOutboxClient([
        {
            "id": 10,
            "project_id": "test-proj",
            "commit_id": "a" * 40,
            "event_type": "version_committed",
            "payload": {"scope_hash": "b" * 40, "conflicts": 0},
            "attempts": 1,
        }
    ])
    repo_manager = object()

    processed = process_version_outbox_batch(
        repo_manager=repo_manager,
        client=client,
        limit=10,
    )

    assert processed == 1
    assert client.completed == [10]
    assert client.failed == []
    assert calls == [
        (
            "test-proj",
            repo_manager,
            {
                "status": "ok",
                "commit_id": "a" * 40,
                "root": "b" * 40,
                "merged": False,
                "conflicts": 0,
            },
            True,
        )
    ]


def test_version_outbox_worker_routes_project_rows_to_project_hook(monkeypatch):
    project_calls = []
    scope_calls = []

    def fake_project_hook(project_id, repo_manager, push_result, *, raise_errors=False):
        project_calls.append((project_id, repo_manager, push_result, raise_errors))

    def fake_scope_hook(project_id, repo_manager, push_result, *, raise_errors=False):
        scope_calls.append((project_id, repo_manager, push_result, raise_errors))

    monkeypatch.setattr(
        "src.version_engine.derived.outbox.run_post_project_update_hook",
        fake_project_hook,
    )
    monkeypatch.setattr(
        "src.version_engine.derived.outbox.run_post_push_hook",
        fake_scope_hook,
    )
    client = _FakeOutboxClient([
        {
            "id": 12,
            "project_id": "test-proj",
            "commit_id": "e" * 40,
            "event_type": "project_version_committed",
            "payload": {
                "root_hash": "f" * 40,
                "scope_hash": "0" * 40,
                "conflicts": 0,
            },
            "attempts": 1,
        }
    ])
    repo_manager = object()

    processed = process_version_outbox_batch(
        repo_manager=repo_manager,
        client=client,
        limit=10,
    )

    assert processed == 1
    assert client.completed == [12]
    assert client.failed == []
    assert scope_calls == []
    assert project_calls == [
        (
            "test-proj",
            repo_manager,
            {
                "status": "ok",
                "commit_id": "e" * 40,
                "root": "f" * 40,
                "merged": False,
                "conflicts": 0,
            },
            True,
        )
    ]


def test_version_outbox_worker_marks_failure_for_retry(monkeypatch):
    def fake_hook(*_args, **_kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(
        "src.version_engine.derived.outbox.run_post_push_hook",
        fake_hook,
    )
    client = _FakeOutboxClient([
        {
            "id": 11,
            "project_id": "test-proj",
            "commit_id": "c" * 40,
            "event_type": "version_committed",
            "payload": {"scope_hash": "d" * 40},
            "attempts": 1,
        }
    ])

    processed = process_version_outbox_batch(
        repo_manager=object(),
        client=client,
        limit=10,
    )

    assert processed == 0
    assert client.completed == []
    assert client.failed == [(11, "projection unavailable")]


def test_git_project_receive_pack_requires_credentials(
    tmp_path, repo_manager, server_repo,
):
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager
    body, client_commit_id = _make_git_receive_pack_body(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/git/test-proj.git/git-receive-pack",
            content=body,
            headers={"content-type": "application/x-git-receive-pack-request"},
        )

    assert client_commit_id
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Git credentials"
    assert response.headers["www-authenticate"] == 'Basic realm="PuppyOne Git"'
    assert server_repo.get_scope_head_commit_id("") == ""


def test_git_ap_info_refs_advertises_receive_pack(monkeypatch, repo_manager, server_repo):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with TestClient(app) as client:
        response = client.get(
            "/git/ap/test-key.git/info/refs",
            params={"service": "git-receive-pack"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/x-git-receive-pack-advertisement",
    )
    assert b"# service=git-receive-pack" in response.content


def test_git_ap_health_reports_degraded_history(monkeypatch, repo_manager, server_repo):
    server_repo.add_scope("docs-scope", "/docs/")
    malformed_tree = server_repo.store.put_tree(
        b"40000 broken\x00" + bytes.fromhex("28a44dbeee08f49e"),
    )
    bad_commit = _make_client_commit(
        server_repo,
        malformed_tree,
        message="legacy malformed tree",
    )
    current_tree = build_tree_from_files(server_repo.store, {"README.md": b"current\n"})
    current_commit = _make_client_commit(
        server_repo,
        current_tree,
        parent_id=bad_commit,
        message="current after legacy damage",
    )
    server_repo.history.set_scope_hash("docs", current_tree)
    server_repo.set_scope_head_commit_id("docs", current_commit)
    projected = git_view_head_commit(server_repo, "docs")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with TestClient(app) as client:
        response = client.get("/git/ap/test-key.git/health")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["health"] == "history_degraded"
    assert data["git_usable"] is True
    assert data["clone_usable"] is True
    assert data["fetch_usable"] is True
    assert data["push_usable"] is True
    assert data["git_head"] == projected
    assert data["canonical_head"] == current_commit
    assert data["history_cut"] is True
    assert [item["type"] for item in data["recommended_actions"]] == [
        "continue",
        "repair_history",
    ]


def test_git_ap_health_reports_current_corrupt_and_info_refs_returns_409(
    monkeypatch,
    repo_manager,
    server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    malformed_tree = server_repo.store.put_tree(
        b"40000 broken\x00" + bytes.fromhex("28a44dbeee08f49e"),
    )
    current_commit = _make_client_commit(
        server_repo,
        malformed_tree,
        message="current damaged tree",
    )
    server_repo.history.set_scope_hash("docs", malformed_tree)
    server_repo.set_scope_head_commit_id("docs", current_commit)
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with TestClient(app) as client:
        health_response = client.get("/git/ap/test-key.git/health")
        refs_response = client.get(
            "/git/ap/test-key.git/info/refs",
            params={"service": "git-upload-pack"},
        )

    assert health_response.status_code == 200
    data = health_response.json()["data"]
    assert data["health"] == "current_corrupt"
    assert data["git_usable"] is False
    assert data["clone_usable"] is False
    assert data["fetch_usable"] is False
    assert data["push_usable"] is False
    assert data["git_head"] == ""
    assert data["canonical_head"] == current_commit
    assert [item["type"] for item in data["recommended_actions"]] == [
        "restore_version",
        "repair_storage",
        "rebuild_cache",
    ]

    assert refs_response.status_code == 409
    assert refs_response.json()["detail"]["error_code"] == "GIT_VIEW_CURRENT_CORRUPT"


def test_git_access_point_receive_pack_uses_bound_scope_and_identity(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(user_identity="alice@example.com"),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager
    body, client_commit_id = _make_git_receive_pack_body(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/git/ap/test-key.git/git-receive-pack",
            content=body,
            headers={
                "content-type": "application/x-git-receive-pack-request",
                "x-puppyone-user": "alice@example.com",
            },
        )

    assert response.status_code == 200
    assert b"unpack ok" in response.content
    assert b"ok refs/heads/main" in response.content
    assert server_repo.get_scope_head_commit_id("docs") == client_commit_id
    root_projection_head = server_repo.get_scope_head_commit_id("")
    assert root_projection_head
    assert root_projection_head != client_commit_id
    assert _files_for_scope(server_repo) == {"docs/README.md": b"hello from git\n"}
    assert _files_for_scope(server_repo, "docs") == {"README.md": b"hello from git\n"}
    assert _last_audit_event(server_repo, "access_git_push")["agent"] == "alice@example.com"


def test_git_access_point_receive_pack_accepts_flush_only_noop(
    monkeypatch, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with TestClient(app) as client:
        response = client.post(
            "/git/ap/test-key.git/git-receive-pack",
            content=b"0000",
            headers={"content-type": "application/x-git-receive-pack-request"},
        )

    assert response.status_code == 200
    assert response.content == b""
    assert server_repo.get_scope_head_commit_id("docs") == ""


def test_post_push_hook_warms_affected_git_transport_views(
    monkeypatch, repo_manager, server_repo,
):
    from src.version_engine.derived.hooks import run_post_push_hook

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_warm(_repo, scope_path: str, scope_excludes=None):
        calls.append((scope_path, tuple(scope_excludes or [])))
        return "warm-head"

    monkeypatch.setattr(
        "src.version_engine.derived.git_transport_cache.warm_git_transport_view",
        fake_warm,
    )

    server_repo.add_scope("root-scope", "")
    server_repo.add_scope("docs-scope", "/docs/", exclude=["/docs/private/"])
    tree_id = build_tree_from_files(server_repo.store, {"README.md": b"docs\n"})
    commit_id = _make_client_commit(server_repo, tree_id, message="docs update")
    server_repo.history.set_scope_hash("docs", tree_id)
    server_repo.set_scope_head_commit_id("docs", commit_id)
    server_repo.record_history(
        commit_id,
        "git:user",
        "docs update",
        "docs",
        [{"path": "README.md", "action": "modify"}],
        scope_hash=tree_id,
        created_at_iso="2026-01-01T00:00:00+00:00",
    )

    run_post_push_hook(
        "test-proj",
        repo_manager,
        {"status": "ok", "commit_id": commit_id, "root": tree_id},
        raise_errors=True,
    )

    assert ("", ()) in calls
    assert ("docs", ("docs/private",)) in calls


def test_git_access_point_rejects_bound_identity_mismatch(
    monkeypatch, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(user_identity="alice@example.com"),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with TestClient(app) as client:
        response = client.get(
            "/git/ap/test-key.git/info/refs",
            params={"service": "git-receive-pack"},
            headers={"x-puppyone-user": "bob@example.com"},
        )

    assert response.status_code == 401
    assert "different user" in response.json()["detail"]


def test_git_access_point_readonly_push_is_rejected(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(mode="r"),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager
    body, _client_commit_id = _make_git_receive_pack_body(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/git/ap/test-key.git/git-receive-pack",
            content=body,
            headers={"content-type": "application/x-git-receive-pack-request"},
        )

    assert response.status_code == 200
    # E4/E6: ng line now carries the structured "puppyone-rejected:" tag
    # so tooling can disambiguate the rejection class.
    assert b"ng refs/heads/main puppyone-rejected: access point is read-only" in response.content
    assert server_repo.get_scope_head_commit_id("docs") == ""
    assert server_repo.audit.events == []


def test_real_git_cli_can_clone_commit_push_and_clone_again(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        first = tmp_path / "first"
        second = tmp_path / "second"

        _run_git(["clone", remote, str(first)], tmp_path)
        _configure_git_identity(first)
        (first / "README.md").write_text("hello through real git\n", encoding="utf-8")
        _run_git(["add", "README.md"], first)
        _run_git(["commit", "-m", "real git smoke"], first)
        pushed_head = _run_git(["rev-parse", "HEAD"], first).decode("ascii").strip()
        _run_git(["push", "origin", "main"], first)

        _run_git(["clone", remote, str(second)], tmp_path)

    assert server_repo.get_scope_head_commit_id("docs") == pushed_head
    assert _files_for_scope(server_repo, "docs") == {
        "README.md": b"hello through real git\n",
    }
    assert (second / "README.md").read_text(encoding="utf-8") == "hello through real git\n"
    assert _last_audit_event(server_repo, "access_git_push")["type"] == "access_git_push"


@pytest.mark.parametrize(
    ("scope_id", "scope_path", "remote_path", "expected_scope"),
    [
        ("root-scope", "", "/git/test-proj.git", ""),
        (
            "docs-scope",
            "/docs/",
            "/git/test-proj/scopes/docs-scope.git",
            "docs",
        ),
    ],
    ids=["project-root", "scoped"],
)
def test_real_git_cli_uses_canonical_locator_and_credential_helper(
    monkeypatch,
    tmp_path,
    repo_manager,
    server_repo,
    scope_id,
    scope_path,
    remote_path,
    expected_scope,
):
    """Stock Git authenticates without a key-bearing URL on both locators."""

    server_repo.add_scope(scope_id, scope_path)
    if not scope_path:
        _init_empty_project_shell(server_repo)
    _patch_canonical_git_credentials(
        monkeypatch,
        scope_id=scope_id,
        scope_path=scope_path,
        is_root=not bool(scope_path),
    )
    credential_env = _standard_git_credential_env(tmp_path, "git_secret")
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}{remote_path}"
        first = tmp_path / "canonical-first"
        second = tmp_path / "canonical-second"

        _run_git(["clone", remote, str(first)], tmp_path, env=credential_env)
        assert "git_secret" not in _run_git(
            ["remote", "get-url", "origin"], first
        ).decode("utf-8")
        _configure_git_identity(first)
        (first / "README.md").write_text(
            f"canonical {scope_id}\n",
            encoding="utf-8",
        )
        _run_git(["add", "README.md"], first)
        _run_git(["commit", "-m", "canonical Git smoke"], first)
        pushed_head = _run_git(["rev-parse", "HEAD"], first).decode("ascii").strip()
        _run_git(["push", "origin", "main"], first, env=credential_env)
        _run_git(["clone", remote, str(second)], tmp_path, env=credential_env)

    assert server_repo.get_scope_head_commit_id(expected_scope) == pushed_head
    assert _files_for_scope(server_repo, expected_scope) == {
        "README.md": f"canonical {scope_id}\n".encode(),
    }
    assert (second / "README.md").read_text(encoding="utf-8") == (
        f"canonical {scope_id}\n"
    )


def test_canonical_and_legacy_git_routes_share_one_scope_and_canonical_cas(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_canonical_git_credentials(
        monkeypatch,
        scope_id="docs-scope",
        scope_path="/docs/",
        is_root=False,
    )
    _patch_git_scope_auth(
        monkeypatch,
        {"legacy-key": ("docs-scope", "/docs/", "rw")},
    )
    credential_env = _standard_git_credential_env(tmp_path, "git_secret")
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        canonical = f"{base_url}/git/test-proj/scopes/docs-scope.git"
        legacy = f"{base_url}/git/ap/legacy-key.git"
        canonical_work = tmp_path / "canonical-work"
        legacy_work = tmp_path / "legacy-work"
        canonical_verify = tmp_path / "canonical-verify"

        _run_git(
            ["clone", canonical, str(canonical_work)],
            tmp_path,
            env=credential_env,
        )
        _configure_git_identity(canonical_work)
        (canonical_work / "README.md").write_text("canonical v1\n", encoding="utf-8")
        _run_git(["add", "README.md"], canonical_work)
        _run_git(["commit", "-m", "canonical v1"], canonical_work)
        _run_git(["push", "origin", "main"], canonical_work, env=credential_env)

        _run_git(["clone", legacy, str(legacy_work)], tmp_path)
        assert (legacy_work / "README.md").read_text(encoding="utf-8") == "canonical v1\n"
        _configure_git_identity(legacy_work)
        (legacy_work / "README.md").write_text("legacy v2\n", encoding="utf-8")
        _run_git(["add", "README.md"], legacy_work)
        _run_git(["commit", "-m", "legacy v2"], legacy_work)
        final_head = _run_git(["rev-parse", "HEAD"], legacy_work).decode().strip()
        _run_git(["push", "origin", "main"], legacy_work)

        _run_git(
            ["clone", canonical, str(canonical_verify)],
            tmp_path,
            env=credential_env,
        )

    assert server_repo.get_scope_head_commit_id("docs") == final_head
    assert _files_for_scope(server_repo, "docs") == {"README.md": b"legacy v2\n"}
    assert _files_for_scope(server_repo) == {"docs/README.md": b"legacy v2\n"}
    assert (canonical_verify / "README.md").read_text(encoding="utf-8") == "legacy v2\n"
    push_events = [
        event
        for event in server_repo.audit.events
        if event.get("type") == "access_git_push"
    ]
    assert [event["detail"]["entry_point"] for event in push_events] == [
        "project_git_remote",
        "access_key_git_remote",
    ]
    assert {event["detail"]["scope"] for event in push_events} == {"docs"}


def test_real_git_cli_degraded_history_clones_and_pushes_from_projected_head(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    malformed_tree = server_repo.store.put_tree(
        b"40000 broken\x00" + bytes.fromhex("28a44dbeee08f49e"),
    )
    bad_commit = _make_client_commit(
        server_repo,
        malformed_tree,
        message="legacy malformed tree",
    )
    current_tree = build_tree_from_files(server_repo.store, {"README.md": b"current\n"})
    current_commit = _make_client_commit(
        server_repo,
        current_tree,
        parent_id=bad_commit,
        message="current after legacy damage",
    )
    server_repo.history.set_scope_hash("docs", current_tree)
    server_repo.set_scope_head_commit_id("docs", current_commit)
    projected = git_view_head_commit(server_repo, "docs")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        work = tmp_path / "work"

        _run_git(["clone", remote, str(work)], tmp_path)
        assert _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip() == projected
        _configure_git_identity(work)
        (work / "README.md").write_text("current plus push\n", encoding="utf-8")
        _run_git(["add", "README.md"], work)
        _run_git(["commit", "-m", "push after degraded history"], work)
        pushed_head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

    assert server_repo.get_scope_head_commit_id("docs") == pushed_head
    assert _files_for_scope(server_repo, "docs") == {
        "README.md": b"current plus push\n",
    }
    assert _last_audit_event(server_repo, "access_git_push")["detail"][
        "git_visible_old_commit_id"
    ] == projected
    assert _last_audit_event(server_repo, "access_git_push")["detail"][
        "canonical_base_commit_id"
    ] == current_commit
    assert _last_audit_event(server_repo, "access_git_push")["detail"]["git_view_history_cut"] is True


def test_real_git_cli_root_derived_scope_without_scope_state_pushes(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    """A root-first scope view can be valid before its cache row exists.

    Git advertises a synthetic scope-view commit derived from the root tree.
    Pushing from that advertised commit must not be translated to an empty
    canonical scope head and rejected as non-fast-forward.
    """

    scope_path = "New Folder (2)"
    server_repo.add_scope("new-folder-scope", f"/{scope_path}/")
    scope_tree = build_tree_from_files(
        server_repo.store,
        {"README.md": b"root-derived scope\n"},
    )
    root_tree = build_tree_from_files(
        server_repo.store,
        {f"{scope_path}/README.md": b"root-derived scope\n"},
    )
    root_commit = _make_client_commit(
        server_repo,
        root_tree,
        message="root contains scope",
    )
    server_repo.history.set_root_hash(root_tree)
    server_repo.history.set_scope_hash("", root_tree)
    server_repo.set_scope_head_commit_id("", root_commit)
    projected = git_view_head_commit(server_repo, scope_path)
    assert projected
    assert commit_tree_id(server_repo, projected) == scope_tree
    assert server_repo.get_scope_head_commit_id(scope_path) == ""

    _patch_git_scope_auth(
        monkeypatch,
        {"test-key": ("new-folder-scope", f"/{scope_path}/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        work = tmp_path / "work"

        _run_git(["clone", remote, str(work)], tmp_path)
        assert _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip() == projected
        _configure_git_identity(work)
        (work / "perf-git-access-point.md").write_text(
            "root-derived access point push\n",
            encoding="utf-8",
        )
        _run_git(["add", "perf-git-access-point.md"], work)
        _run_git(["commit", "-m", "push from root-derived scope"], work)
        pushed_head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

    assert server_repo.get_scope_head_commit_id(scope_path) == pushed_head
    assert server_repo.list_scope_files(
        {"id": "new-folder-scope", "path": scope_path, "exclude": []}
    ) == {
        "README.md": b"root-derived scope\n",
        "perf-git-access-point.md": b"root-derived access point push\n",
    }
    detail = _last_audit_event(server_repo, "access_git_push")["detail"]
    assert detail["git_visible_old_commit_id"] == projected
    assert detail["canonical_base_commit_id"] == ""
    assert detail["engine_base_commit_id"] == projected


def test_project_visibility_barrier_keeps_source_scope_head(
    monkeypatch,
    repo_manager,
    server_repo,
):
    scope_path = "docs"
    server_repo.add_scope("docs-scope", f"/{scope_path}/")

    old_scope_tree = build_tree_from_files(
        server_repo.store,
        {"README.md": b"old\n"},
    )
    old_root_tree = build_tree_from_files(
        server_repo.store,
        {f"{scope_path}/README.md": b"old\n"},
    )
    old_root_commit = _make_client_commit(server_repo, old_root_tree)
    server_repo.history.set_root_hash(old_root_tree)
    server_repo.history.set_scope_hash("", old_root_tree)
    server_repo.set_scope_head_commit_id("", old_root_commit)
    server_repo.history.set_scope_hash(scope_path, old_scope_tree)
    server_repo.set_scope_head_commit_id(
        scope_path,
        _make_client_commit(server_repo, old_scope_tree),
    )

    client_scope_tree = build_tree_from_files(
        server_repo.store,
        {
            "README.md": b"new\n",
            "guide.md": b"published from git\n",
        },
    )
    client_commit = _make_client_commit(
        server_repo,
        client_scope_tree,
        message="client scope commit",
    )
    new_root_tree = build_tree_from_files(
        server_repo.store,
        {
            f"{scope_path}/README.md": b"new\n",
            f"{scope_path}/guide.md": b"published from git\n",
        },
    )
    root_commit = _make_client_commit(
        server_repo,
        new_root_tree,
        parent_id=old_root_commit,
        message="root commit",
    )

    published, _txn_id = server_repo.publish_project_update(
        old_root_hash=old_root_tree,
        new_root_hash=new_root_tree,
        scope_path=scope_path,
        scope_hash=client_scope_tree,
        scope_head_commit_id=client_commit,
        commit_id=root_commit,
        who="git:user",
        message="publish scoped git tree",
        changes=[
            {"path": f"{scope_path}/README.md", "action": "update"},
            {"path": f"{scope_path}/guide.md", "action": "add"},
        ],
        conflicts=[],
        created_at_iso="2026-01-01T00:00:00+00:00",
        audit_event_type="git_push",
        audit_agent_id="git:user",
        audit_detail={},
        source_channel="git",
        base_commit_id=old_root_commit,
        client_commit_id=client_commit,
        proposed_tree_id=client_scope_tree,
        intent_type="submission",
    )
    assert published

    calls: list[str] = []
    original_cas = derived_hooks._cas_or_set_scope_state

    def spy_cas(repo, scope, old_hash, new_hash, head_commit_id):
        calls.append(scope)
        return original_cas(repo, scope, old_hash, new_hash, head_commit_id)

    monkeypatch.setattr(derived_hooks, "_cas_or_set_scope_state", spy_cas)

    derived_hooks.run_project_root_visibility_barrier(
        "test-proj",
        repo_manager,
        {
            "status": "ok",
            "commit_id": root_commit,
            "root": new_root_tree,
            "old_root": old_root_tree,
            "scope_path": scope_path,
            "scope_hash": client_scope_tree,
        },
        raise_errors=True,
    )

    assert scope_path not in calls
    assert server_repo.get_scope_head_commit_id(scope_path) == client_commit
    assert server_repo.get_scope_hash(scope_path) == client_scope_tree


def test_project_visibility_barrier_refreshes_nested_scope_views(
    repo_manager,
    server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    server_repo.add_scope("api-scope", "/docs/api/")
    old_root = build_tree_from_files(
        server_repo.store,
        {
            "docs/guide.md": b"guide v0\n",
            "docs/api/ref.md": b"api v0\n",
        },
    )
    old_docs = build_tree_from_files(
        server_repo.store,
        {
            "guide.md": b"guide v0\n",
            "api/ref.md": b"api v0\n",
        },
    )
    old_api = build_tree_from_files(
        server_repo.store,
        {"ref.md": b"api v0\n"},
    )
    old_root_commit = _make_client_commit(server_repo, old_root, message="root v0")
    docs_commit = _make_client_commit(server_repo, old_docs, message="docs v0")
    api_commit = _make_client_commit(server_repo, old_api, message="api v0")
    server_repo.history.set_root_hash(old_root)
    server_repo.history.set_scope_hash("", old_root)
    server_repo.set_scope_head_commit_id("", old_root_commit)
    server_repo.history.set_scope_hash("docs", old_docs)
    server_repo.set_scope_head_commit_id("docs", docs_commit)
    server_repo.history.set_scope_hash("docs/api", old_api)
    server_repo.set_scope_head_commit_id("docs/api", api_commit)

    new_root = build_tree_from_files(
        server_repo.store,
        {
            "docs/guide.md": b"guide v0\n",
            "docs/api/ref.md": b"api v1\n",
        },
    )
    new_root_commit = _make_client_commit(
        server_repo,
        new_root,
        parent_id=old_root_commit,
        message="root updates nested api",
    )
    published, _txn_id = server_repo.publish_project_update(
        old_root_hash=old_root,
        new_root_hash=new_root,
        scope_path="",
        scope_hash=new_root,
        scope_head_commit_id="",
        commit_id=new_root_commit,
        who="web:user",
        message="root updates nested api",
        changes=[
            {"path": "docs/api/ref.md", "action": "update"},
        ],
        conflicts=[],
        created_at_iso="2026-01-01T00:00:00+00:00",
        audit_event_type="web_push",
        audit_agent_id="web:user",
        audit_detail={},
        source_channel="papi",
        policy="lww",
        base_commit_id=old_root_commit,
        client_commit_id=new_root_commit,
        proposed_tree_id=new_root,
        intent_type="operation",
    )
    assert published

    derived_hooks.run_project_root_visibility_barrier(
        "test-proj",
        repo_manager,
        {
            "status": "ok",
            "commit_id": new_root_commit,
            "root": new_root,
            "old_root": old_root,
        },
        raise_errors=True,
    )

    assert _files_for_scope(server_repo, "docs") == {
        "guide.md": b"guide v0\n",
        "api/ref.md": b"api v1\n",
    }
    assert _files_for_scope(server_repo, "docs/api") == {
        "ref.md": b"api v1\n",
    }
    docs_head = server_repo.get_scope_head_commit_id("docs")
    api_head = server_repo.get_scope_head_commit_id("docs/api")
    assert docs_head != docs_commit
    assert api_head != api_commit
    assert commit_tree_id(server_repo, docs_head) == server_repo.get_scope_hash("docs")
    assert commit_tree_id(server_repo, api_head) == server_repo.get_scope_hash("docs/api")


def test_real_git_cli_first_push_to_empty_project_shell(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("root-scope", "")
    _init_empty_project_shell(server_repo)
    _patch_git_scope_auth(
        monkeypatch,
        {"root-key": ("root-scope", "", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/root-key.git"
        work = tmp_path / "existing"
        verify = tmp_path / "verify"
        work.mkdir()
        _run_git(["init"], work)
        _configure_git_identity(work)
        (work / "README.md").write_text("first project content\n", encoding="utf-8")
        _run_git(["add", "README.md"], work)
        _run_git(["commit", "-m", "first content"], work)
        _run_git(["branch", "-M", "main"], work)
        pushed_head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["remote", "add", "origin", remote], work)
        _run_git(["push", "-u", "origin", "main"], work)

        _run_git(["clone", remote, str(verify)], tmp_path)

    assert server_repo.get_scope_head_commit_id("") == pushed_head
    assert server_repo.get_head_commit_id() == pushed_head
    assert _files_for_scope(server_repo) == {"README.md": b"first project content\n"}
    assert (verify / "README.md").read_text(encoding="utf-8") == "first project content\n"


def test_real_git_cli_large_first_push_uses_chunked_http_transport(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("root-scope", "")
    _init_empty_project_shell(server_repo)
    _patch_git_scope_auth(
        monkeypatch,
        {"root-key": ("root-scope", "", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/root-key.git"
        work = tmp_path / "large-existing"
        verify = tmp_path / "large-verify"
        work.mkdir()
        _run_git(["init"], work)
        _configure_git_identity(work)
        (work / "README.md").write_text("large import\n", encoding="utf-8")
        large = (b"0123456789abcdef" * (3 * 1024 * 1024 // 16))
        (work / "large.bin").write_bytes(large)
        _run_git(["add", "README.md", "large.bin"], work)
        _run_git(["commit", "-m", "large initial content"], work)
        _run_git(["branch", "-M", "main"], work)
        pushed_head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["remote", "add", "origin", remote], work)
        _run_git(["-c", "http.postBuffer=1024", "push", "-u", "origin", "main"], work)

        _run_git(["clone", remote, str(verify)], tmp_path)
        _run_git(["fsck", "--full"], verify)

    assert server_repo.get_scope_head_commit_id("") == pushed_head
    assert _files_for_scope(server_repo)["README.md"] == b"large import\n"
    assert len(_files_for_scope(server_repo)["large.bin"]) == len(large)
    assert (verify / "large.bin").stat().st_size == len(large)


def test_real_git_cli_tag_push_is_stored_without_advancing_scope(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    # GAP-3: a tag push is no longer rejected — it is stored as a named ref
    # (version_refs) and its objects are promoted so the tag is fetchable —
    # WITHOUT advancing the scope head. (Like all real-git-cli tests this
    # needs the backing services up; deployed verification confirmed the
    # accept+advertise+fetch contract.)
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    import src.version_engine.infrastructure.supabase.version_ref_repository as _vrr
    stored_refs = []

    class _FakeRefStore:
        def list_refs(self, *a, **k):
            return []

        def set_ref(self, **kw):
            stored_refs.append(kw)
            return True

    monkeypatch.setattr(_vrr, "VersionRefStore", lambda *a, **k: _FakeRefStore())
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "tag-client"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "README.md").write_text("tag target\n", encoding="utf-8")
        _run_git(["add", "README.md"], work)
        _run_git(["commit", "-m", "tag target"], work)
        _run_git(["tag", "v1"], work)
        tagged_head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        proc = _run_git_raw(["push", "origin", "v1"], work)

    assert proc.returncode == 0, proc.stderr          # tag push accepted + stored
    assert server_repo.get_scope_head_commit_id("docs") == ""   # scope head NOT advanced
    assert server_repo.store.exists(tagged_head)      # tag objects promoted (fetchable)
    assert stored_refs == [{
        "project_id": "test-proj",
        "scope_path": "docs",
        "ref_name": "refs/tags/v1",
        "commit_id": tagged_head,
        "created_by": "scope:docs-scope",
    }]


def test_real_git_cli_lfs_pointer_push_is_rejected_without_advancing_scope(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "lfs-client"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "asset.bin").write_text(
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{'a' * 64}\n"
            "size 12345\n",
            encoding="utf-8",
        )
        _run_git(["add", "asset.bin"], work)
        _run_git(["commit", "-m", "lfs pointer"], work)
        pointer_commit = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        proc = _run_git_raw(["push", "origin", "main"], work)

    assert proc.returncode != 0
    assert b"Git LFS is not supported" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") == ""
    assert not server_repo.store.exists(pointer_commit)


def test_real_git_cli_stale_force_push_is_rejected_like_git_hosts(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"test-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        first = tmp_path / "first"
        second = tmp_path / "second"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(first)], tmp_path)
        _run_git(["clone", remote, str(second)], tmp_path)
        _configure_git_identity(first)
        _configure_git_identity(second)

        (first / "a.txt").write_text("from first\n", encoding="utf-8")
        _run_git(["add", "a.txt"], first)
        _run_git(["commit", "-m", "first adds a"], first)
        _run_git(["push", "origin", "main"], first)

        (second / "b.txt").write_text("from stale second\n", encoding="utf-8")
        _run_git(["add", "b.txt"], second)
        _run_git(["commit", "-m", "second adds b from stale base"], second)
        stale_head = _run_git(["rev-parse", "HEAD"], second).decode("ascii").strip()
        proc = _run_git_raw(["push", "--force", "origin", "main"], second)

        _run_git(["clone", remote, str(verify)], tmp_path)

    assert proc.returncode != 0
    assert b"non-fast-forward" in proc.stderr or b"fetch first" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") != stale_head
    assert _files_for_scope(server_repo, "docs") == {
        "a.txt": b"from first\n",
    }
    assert (verify / "a.txt").read_text(encoding="utf-8") == "from first\n"
    assert not (verify / "b.txt").exists()
    assert _last_audit_event(server_repo, "access_git_push")["detail"]["merged"] is False


def test_real_git_cli_scoped_remotes_do_not_leak_sibling_worktrees(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    server_repo.add_scope("src-scope", "/src/")
    _patch_git_scope_auth(
        monkeypatch,
        {
            "docs-key": ("docs-scope", "/docs/", "rw"),
            "src-key": ("src-scope", "/src/", "rw"),
        },
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        docs_remote = f"{base_url}/git/ap/docs-key.git"
        src_remote = f"{base_url}/git/ap/src-key.git"
        docs = tmp_path / "docs"
        src = tmp_path / "src"
        docs_again = tmp_path / "docs-again"
        src_again = tmp_path / "src-again"

        _run_git(["clone", docs_remote, str(docs)], tmp_path)
        _configure_git_identity(docs)
        (docs / "README.md").write_text("docs only\n", encoding="utf-8")
        _run_git(["add", "README.md"], docs)
        _run_git(["commit", "-m", "docs"], docs)
        _run_git(["push", "origin", "main"], docs)

        _run_git(["clone", src_remote, str(src)], tmp_path)
        assert not (src / "README.md").exists()
        _configure_git_identity(src)
        (src / "app.py").write_text("print('src only')\n", encoding="utf-8")
        _run_git(["add", "app.py"], src)
        _run_git(["commit", "-m", "src"], src)
        _run_git(["push", "origin", "main"], src)

        _run_git(["clone", docs_remote, str(docs_again)], tmp_path)
        _run_git(["clone", src_remote, str(src_again)], tmp_path)

    assert (docs_again / "README.md").read_text(encoding="utf-8") == "docs only\n"
    assert not (docs_again / "app.py").exists()
    assert (src_again / "app.py").read_text(encoding="utf-8") == "print('src only')\n"
    assert not (src_again / "README.md").exists()
    assert server_repo.get_scope_head_commit_id("docs")
    assert server_repo.get_scope_head_commit_id("src")


def test_real_git_cli_multi_commit_push_preserves_commit_chain(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"test-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        work = tmp_path / "work"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "one.txt").write_text("one\n", encoding="utf-8")
        _run_git(["add", "one.txt"], work)
        _run_git(["commit", "-m", "one"], work)
        first_commit = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        (work / "two.txt").write_text("two\n", encoding="utf-8")
        _run_git(["add", "two.txt"], work)
        _run_git(["commit", "-m", "two"], work)
        second_commit = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

        _run_git(["clone", remote, str(verify)], tmp_path)
        log = _run_git(["log", "--format=%H:%s"], verify).decode("utf-8")

    assert server_repo.get_scope_head_commit_id("docs") == second_commit
    assert _files_for_scope(server_repo, "docs") == {
        "one.txt": b"one\n",
        "two.txt": b"two\n",
    }
    assert f"{second_commit}:two" in log
    assert f"{first_commit}:one" in log


def test_real_git_cli_readonly_access_point_can_clone_but_push_is_rejected(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"test-key": ("docs-scope", "/docs/", "r")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        work = tmp_path / "readonly"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "README.md").write_text("should not land\n", encoding="utf-8")
        _run_git(["add", "README.md"], work)
        _run_git(["commit", "-m", "blocked"], work)
        proc = _run_git_raw(["push", "origin", "main"], work)

    assert proc.returncode != 0
    assert b"access point is read-only" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") == ""
    assert server_repo.audit.events == []


def test_real_git_cli_scoped_remote_rejects_unadvertised_sibling_object_fetch(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    server_repo.add_scope("src-scope", "/src/")
    _patch_git_scope_auth(
        monkeypatch,
        {
            "docs-key": ("docs-scope", "/docs/", "rw"),
            "src-key": ("src-scope", "/src/", "rw"),
        },
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        docs_remote = f"{base_url}/git/ap/docs-key.git"
        src_remote = f"{base_url}/git/ap/src-key.git"
        src = tmp_path / "src"
        docs = tmp_path / "docs"

        _run_git(["clone", src_remote, str(src)], tmp_path)
        _configure_git_identity(src)
        (src / "secret.txt").write_text("src secret\n", encoding="utf-8")
        _run_git(["add", "secret.txt"], src)
        _run_git(["commit", "-m", "src secret"], src)
        src_commit = _run_git(["rev-parse", "HEAD"], src).decode("ascii").strip()
        _run_git(["push", "origin", "main"], src)

        _run_git(["clone", docs_remote, str(docs)], tmp_path)
        proc = _run_git_raw(["fetch", "origin", src_commit], docs)

    assert proc.returncode != 0
    assert not (docs / "secret.txt").exists()


def test_real_git_cli_root_view_grafts_child_scope_worktrees(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("root-scope", "/")
    server_repo.add_scope("docs-scope", "/docs/")
    server_repo.add_scope("src-scope", "/src/")
    _patch_git_scope_auth(
        monkeypatch,
        {
            "root-key": ("root-scope", "/", "rw"),
            "docs-key": ("docs-scope", "/docs/", "rw"),
            "src-key": ("src-scope", "/src/", "rw"),
        },
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        root_remote = f"{base_url}/git/ap/root-key.git"
        docs_remote = f"{base_url}/git/ap/docs-key.git"
        src_remote = f"{base_url}/git/ap/src-key.git"
        docs = tmp_path / "docs"
        src = tmp_path / "src"
        root = tmp_path / "root"

        _run_git(["clone", docs_remote, str(docs)], tmp_path)
        _configure_git_identity(docs)
        (docs / "README.md").write_text("docs graft\n", encoding="utf-8")
        _run_git(["add", "README.md"], docs)
        _run_git(["commit", "-m", "docs graft"], docs)
        _run_git(["push", "origin", "main"], docs)

        _run_git(["clone", src_remote, str(src)], tmp_path)
        _configure_git_identity(src)
        (src / "app.py").write_text("print('graft')\n", encoding="utf-8")
        _run_git(["add", "app.py"], src)
        _run_git(["commit", "-m", "src graft"], src)
        _run_git(["push", "origin", "main"], src)

        _run_git(["clone", root_remote, str(root)], tmp_path)

    assert (root / "docs" / "README.md").read_text(encoding="utf-8") == "docs graft\n"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('graft')\n"


def test_real_git_cli_concurrent_same_scope_force_pushes_are_rejected_or_single_winner(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"test-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        worktrees = []
        for index in range(4):
            work = tmp_path / f"client-{index}"
            _run_git(["clone", remote, str(work)], tmp_path)
            _configure_git_identity(work)
            (work / f"file-{index}.txt").write_text(f"client {index}\n", encoding="utf-8")
            _run_git(["add", f"file-{index}.txt"], work)
            _run_git(["commit", "-m", f"client {index}"], work)
            worktrees.append(work)

        with ThreadPoolExecutor(max_workers=4) as pool:
            pushes = list(
                pool.map(
                    lambda work: _run_git_raw(["push", "--force", "origin", "main"], work),
                    worktrees,
                ),
            )

        verify = tmp_path / "verify"
        _run_git(["clone", remote, str(verify)], tmp_path)

    successful = [index for index, push in enumerate(pushes) if push.returncode == 0]
    rejected = [push for push in pushes if push.returncode != 0]
    assert len(successful) == 1
    assert len(rejected) == 3
    assert all(b"non-fast-forward" in push.stderr for push in rejected)
    winner = successful[0]
    assert _files_for_scope(server_repo, "docs") == {
        f"file-{winner}.txt": f"client {winner}\n".encode()
    }
    assert (verify / f"file-{winner}.txt").read_text(encoding="utf-8") == f"client {winner}\n"


def test_real_git_cli_client_merge_commit_is_rejected(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"test-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/test-key.git"
        work = tmp_path / "merge-client"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git(["add", "base.txt"], work)
        _run_git(["commit", "-m", "base"], work)
        base_commit = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

        _run_git(["checkout", "-b", "feature"], work)
        (work / "feature.txt").write_text("feature\n", encoding="utf-8")
        _run_git(["add", "feature.txt"], work)
        _run_git(["commit", "-m", "feature"], work)

        _run_git(["checkout", "main"], work)
        (work / "main.txt").write_text("main\n", encoding="utf-8")
        _run_git(["add", "main.txt"], work)
        _run_git(["commit", "-m", "main side"], work)
        _run_git(["merge", "--no-ff", "feature", "-m", "client local merge"], work)

        proc = _run_git_raw(["push", "origin", "main"], work)

    assert proc.returncode != 0
    assert b"client merge commits are not supported" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") == base_commit
    assert _files_for_scope(server_repo, "docs") == {"base.txt": b"base\n"}


def test_real_git_cli_root_scope_cannot_write_child_scope_path(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("root-scope", "/")
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"root-key": ("root-scope", "/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/root-key.git"
        work = tmp_path / "root-client"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "docs").mkdir()
        (work / "docs" / "owned-by-docs.md").write_text("bad cross-scope write\n", encoding="utf-8")
        _run_git(["add", "docs/owned-by-docs.md"], work)
        _run_git(["commit", "-m", "bad child write"], work)
        rejected_commit = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        proc = _run_git_raw(["push", "origin", "main"], work)

    assert proc.returncode != 0
    assert b"submission touches paths outside its scope" in proc.stderr
    assert server_repo.get_scope_head_commit_id("") == ""
    assert server_repo.get_scope_head_commit_id("docs") == ""
    assert not server_repo.store.exists(rejected_commit)


def test_real_git_cli_scope_exclude_visible_push_preserves_hidden_files(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/", exclude=["/docs/secret/"])
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw", ["/docs/secret/"])},
    )

    seeded_tree = build_tree_from_files(
        server_repo.store,
        {
            "README.md": b"visible\n",
            "secret/old.md": b"hidden\n",
        },
    )
    seeded_commit = _make_client_commit(
        server_repo,
        seeded_tree,
        message="legacy seeded docs",
    )
    server_repo.history.set_scope_hash("docs", seeded_tree)
    server_repo.set_scope_head_commit_id("docs", seeded_commit)

    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "docs-visible"
        verify = tmp_path / "docs-visible-verify"

        _run_git(["clone", remote, str(work)], tmp_path)
        assert (work / "README.md").read_text(encoding="utf-8") == "visible\n"
        assert not (work / "secret").exists()

        _configure_git_identity(work)
        (work / "README.md").write_text("visible changed\n", encoding="utf-8")
        _run_git(["add", "README.md"], work)
        _run_git(["commit", "-m", "update visible file"], work)
        client_commit = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

        _run_git(["clone", remote, str(verify)], tmp_path)
        assert (verify / "README.md").read_text(encoding="utf-8") == "visible changed\n"
        assert not (verify / "secret").exists()

    # Root-first keeps the canonical scope tree (including hidden files) in
    # scope_hash/root, while the Access Point ref can remain the Git-visible
    # client commit for normal clone/pull continuity.
    assert server_repo.get_scope_head_commit_id("docs") == client_commit
    assert server_repo.get_scope_hash("docs") != commit_tree_id(server_repo, client_commit)
    assert _files_for_scope(server_repo, "docs") == {
        "README.md": b"visible changed\n",
        "secret/old.md": b"hidden\n",
    }


def test_real_git_cli_scope_exclude_stale_push_is_non_fast_forward(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/", exclude=["/docs/secret/"])
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw", ["/docs/secret/"])},
    )

    seeded_tree = build_tree_from_files(
        server_repo.store,
        {
            "README.md": b"visible\n",
            "secret/old.md": b"hidden\n",
        },
    )
    seeded_commit = _make_client_commit(
        server_repo,
        seeded_tree,
        message="legacy seeded docs",
    )
    server_repo.history.set_scope_hash("docs", seeded_tree)
    server_repo.set_scope_head_commit_id("docs", seeded_commit)

    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        first = tmp_path / "first"
        second = tmp_path / "second"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(first)], tmp_path)
        _run_git(["clone", remote, str(second)], tmp_path)

        _configure_git_identity(first)
        (first / "README.md").write_text("first visible change\n", encoding="utf-8")
        _run_git(["add", "README.md"], first)
        _run_git(["commit", "-m", "first visible change"], first)
        first_head = _run_git(["rev-parse", "HEAD"], first).decode("ascii").strip()
        _run_git(["push", "origin", "main"], first)

        _configure_git_identity(second)
        (second / "notes.md").write_text("stale visible change\n", encoding="utf-8")
        _run_git(["add", "notes.md"], second)
        _run_git(["commit", "-m", "stale visible change"], second)
        stale_head = _run_git(["rev-parse", "HEAD"], second).decode("ascii").strip()
        proc = _run_git_raw(["push", "origin", "main"], second)

        _run_git(["clone", remote, str(verify)], tmp_path)

    assert proc.returncode != 0
    assert b"non-fast-forward" in proc.stderr or b"fetch first" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") == first_head
    assert server_repo.get_scope_head_commit_id("docs") != stale_head
    assert _files_for_scope(server_repo, "docs") == {
        "README.md": b"first visible change\n",
        "secret/old.md": b"hidden\n",
    }
    assert (verify / "README.md").read_text(encoding="utf-8") == "first visible change\n"
    assert not (verify / "notes.md").exists()


def test_real_git_cli_scope_exclude_rejects_push_and_filters_clone(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/", exclude=["/docs/secret/"])
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw", ["/docs/secret/"])},
    )

    seeded_tree = build_tree_from_files(
        server_repo.store,
        {
            "README.md": b"visible\n",
            "secret/old.md": b"hidden\n",
        },
    )
    seeded_commit = _make_client_commit(server_repo, seeded_tree, message="legacy seeded docs")
    server_repo.history.set_scope_hash("docs", seeded_tree)
    server_repo.set_scope_head_commit_id("docs", seeded_commit)

    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "docs"

        _run_git(["clone", remote, str(work)], tmp_path)
        assert (work / "README.md").read_text(encoding="utf-8") == "visible\n"
        assert not (work / "secret").exists()
        hidden_fetch = _run_git_raw(["fetch", "origin", seeded_commit], work)

        _configure_git_identity(work)
        (work / "secret").mkdir()
        (work / "secret" / "new.md").write_text("blocked\n", encoding="utf-8")
        _run_git(["add", "secret/new.md"], work)
        _run_git(["commit", "-m", "try excluded path"], work)
        proc = _run_git_raw(["push", "origin", "main"], work)

    assert hidden_fetch.returncode != 0
    assert proc.returncode != 0
    assert b"submission touches paths outside its scope" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") == seeded_commit
    assert _files_for_scope(server_repo, "docs") == {
        "README.md": b"visible\n",
        "secret/old.md": b"hidden\n",
    }


def test_real_git_cli_bound_identity_mismatch_is_rejected_before_clone(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router.resolve_access_point",
        lambda access_key: _git_access_auth(user_identity="alice@example.com"),
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url.replace('http://', 'http://bob:pw@')}/git/ap/docs-key.git"
        work = tmp_path / "mismatch"
        proc = _run_git_raw(["clone", remote, str(work)], tmp_path)

    assert proc.returncode != 0
    assert b"401" in proc.stderr or b"Authentication failed" in proc.stderr
    assert server_repo.get_scope_head_commit_id("docs") == ""


def test_real_git_cli_stale_same_file_force_push_is_non_fast_forward_rejected(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        seed = tmp_path / "seed"
        alice = tmp_path / "alice"
        bob = tmp_path / "bob"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(seed)], tmp_path)
        _configure_git_identity(seed)
        (seed / "shared.txt").write_text("base\n", encoding="utf-8")
        _run_git(["add", "shared.txt"], seed)
        _run_git(["commit", "-m", "base shared"], seed)
        _run_git(["push", "origin", "main"], seed)

        _run_git(["clone", remote, str(alice)], tmp_path)
        _run_git(["clone", remote, str(bob)], tmp_path)
        _configure_git_identity(alice)
        _configure_git_identity(bob)

        (alice / "shared.txt").write_text("alice\n", encoding="utf-8")
        _run_git(["add", "shared.txt"], alice)
        _run_git(["commit", "-m", "alice changes shared"], alice)
        _run_git(["push", "origin", "main"], alice)

        (bob / "shared.txt").write_text("bob\n", encoding="utf-8")
        _run_git(["add", "shared.txt"], bob)
        _run_git(["commit", "-m", "bob changes shared"], bob)
        proc = _run_git_raw(["push", "--force", "origin", "main"], bob)

        _run_git(["clone", remote, str(verify)], tmp_path)

    assert proc.returncode != 0
    assert b"non-fast-forward" in proc.stderr
    assert (verify / "shared.txt").read_text(encoding="utf-8") == "alice\n"
    assert [event["type"] for event in server_repo.audit.events].count(
        "git_push_conflict_pending"
    ) == 0


def test_real_git_cli_peer_fetch_and_pull_after_push(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        alice = tmp_path / "alice"
        bob = tmp_path / "bob"

        _run_git(["clone", remote, str(alice)], tmp_path)
        _run_git(["clone", remote, str(bob)], tmp_path)
        _configure_git_identity(alice)
        _configure_git_identity(bob)

        (alice / "notes.md").write_text("from alice\n", encoding="utf-8")
        _run_git(["add", "notes.md"], alice)
        _run_git(["commit", "-m", "alice notes"], alice)
        alice_head = _run_git(["rev-parse", "HEAD"], alice).decode("ascii").strip()
        _run_git(["push", "origin", "main"], alice)

        _run_git(["fetch", "origin", "main"], bob)
        fetched = _run_git(["rev-parse", "origin/main"], bob).decode("ascii").strip()
        _run_git(["pull", "--ff-only", "origin", "main"], bob)

    assert fetched == alice_head
    assert (bob / "notes.md").read_text(encoding="utf-8") == "from alice\n"


def test_real_git_cli_rapid_sequential_pushes_keep_log_chain(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "rapid"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        expected = []
        for index in range(8):
            (work / f"r{index}.txt").write_text(f"value {index}\n", encoding="utf-8")
            _run_git(["add", f"r{index}.txt"], work)
            _run_git(["commit", "-m", f"rapid {index}"], work)
            expected.append(_run_git(["rev-parse", "HEAD"], work).decode("ascii").strip())
            _run_git(["push", "origin", "main"], work)

        _run_git(["clone", remote, str(verify)], tmp_path)
        log = _run_git(["log", "--format=%H:%s"], verify).decode("utf-8")

    assert server_repo.get_scope_head_commit_id("docs") == expected[-1]
    for index, commit_id in enumerate(expected):
        assert f"{commit_id}:rapid {index}" in log
        assert (verify / f"r{index}.txt").read_text(encoding="utf-8") == f"value {index}\n"


def test_real_git_cli_repeated_same_file_push_handles_thin_delta_base(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "work"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)

        original = "".join(f"line {index:04d}: stable content\n" for index in range(2000))
        (work / "notes.md").write_text(original, encoding="utf-8")
        _run_git(["add", "notes.md"], work)
        _run_git(["commit", "-m", "add notes"], work)
        _run_git(["push", "origin", "main"], work)

        updated = original.replace(
            "line 0100: stable content\n",
            "line 0100: updated through thin delta\n",
        )
        (work / "notes.md").write_text(updated, encoding="utf-8")
        _run_git(["add", "notes.md"], work)
        _run_git(["commit", "-m", "update notes"], work)
        second_head = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

        _run_git(["clone", remote, str(verify)], tmp_path)
        _run_git(["fsck", "--full", "--strict"], verify)

    assert server_repo.get_scope_head_commit_id("docs") == second_head
    assert (verify / "notes.md").read_text(encoding="utf-8") == updated


def test_git_receive_pack_stores_non_main_and_rejects_delete_multiple_malformed(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    # GAP-3: a non-main branch/tag is now STORED in version_refs instead of
    # being hard-rejected. Stub the store so this unit test doesn't need
    # Supabase, and record what gets stored to prove the push was routed
    # there rather than into the scope head.
    import src.version_engine.infrastructure.supabase.version_ref_repository as _vrr
    stored_refs = []

    class _FakeRefStore:
        def list_refs(self, *a, **k):
            return []

        def set_ref(self, **kw):
            stored_refs.append(kw)
            return True

    monkeypatch.setattr(_vrr, "VersionRefStore", lambda *a, **k: _FakeRefStore())
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager
    body, client_commit_id = _make_git_receive_pack_body(tmp_path)
    non_main = body.replace(b"refs/heads/main", b"refs/heads/side")
    delete_main = _receive_command_body(client_commit_id, "0" * 40, "refs/heads/main")
    first = (
        f"{'0' * 40} {client_commit_id} refs/heads/main"
        "\0 report-status side-band-64k object-format=sha1\n"
    ).encode("ascii")
    second = f"{'0' * 40} {client_commit_id} refs/heads/other\n".encode("ascii")
    multiple = _pkt_line(first) + _pkt_line(second) + b"0000"

    with TestClient(app) as client:
        non_main_resp = client.post(
            "/git/ap/docs-key.git/git-receive-pack",
            content=non_main,
            headers={"content-type": "application/x-git-receive-pack-request"},
        )
        delete_resp = client.post(
            "/git/ap/docs-key.git/git-receive-pack",
            content=delete_main,
            headers={"content-type": "application/x-git-receive-pack-request"},
        )
        multiple_resp = client.post(
            "/git/ap/docs-key.git/git-receive-pack",
            content=multiple,
            headers={"content-type": "application/x-git-receive-pack-request"},
        )
        malformed_resp = client.post(
            "/git/ap/docs-key.git/git-receive-pack",
            content=b"0003",
            headers={"content-type": "application/x-git-receive-pack-request"},
        )

    # GAP-3: a non-main ref is accepted and stored as a named ref — NOT
    # rejected, and crucially WITHOUT advancing the scope head.
    assert b"puppyone-rejected" not in non_main_resp.content
    assert b"refs/heads/side" in non_main_resp.content
    assert any(r.get("ref_name") == "refs/heads/side" for r in stored_refs), stored_refs
    # delete is still refused; wording cites the rollback API.
    assert b"puppyone-rejected: delete is not supported" in delete_resp.content
    assert multiple_resp.status_code == 400
    assert "one scope-bound ref update" in multiple_resp.json()["detail"]
    assert malformed_resp.status_code == 400
    assert server_repo.get_scope_head_commit_id("docs") == ""


def test_git_receive_pack_malformed_pack_does_not_advance_scope(
    monkeypatch, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager
    fake_commit = "1" * 40
    body = _receive_command_body("0" * 40, fake_commit, "refs/heads/main", b"not a pack")

    with TestClient(app) as client:
        response = client.post(
            "/git/ap/docs-key.git/git-receive-pack",
            content=body,
            headers={"content-type": "application/x-git-receive-pack-request"},
        )

    assert response.status_code == 200
    assert b"ng refs/heads/main" in response.content
    assert server_repo.get_scope_head_commit_id("docs") == ""
    assert server_repo.audit.events == []


@pytest.mark.asyncio
async def test_real_git_cli_rollback_of_git_commits_is_visible_to_git_clone(
    monkeypatch, tmp_path, repo_manager, server_repo,
):
    server_repo.add_scope("docs-scope", "/docs/")
    _patch_git_scope_auth(
        monkeypatch,
        {"docs-key": ("docs-scope", "/docs/", "rw")},
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: repo_manager

    with _serve_git_app(app) as base_url:
        remote = f"{base_url}/git/ap/docs-key.git"
        work = tmp_path / "work"
        verify = tmp_path / "verify"

        _run_git(["clone", remote, str(work)], tmp_path)
        _configure_git_identity(work)
        (work / "doc.md").write_text("v1\n", encoding="utf-8")
        _run_git(["add", "doc.md"], work)
        _run_git(["commit", "-m", "v1"], work)
        v1 = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

        (work / "doc.md").write_text("v2\n", encoding="utf-8")
        _run_git(["add", "doc.md"], work)
        _run_git(["commit", "-m", "v2"], work)
        v2 = _run_git(["rev-parse", "HEAD"], work).decode("ascii").strip()
        _run_git(["push", "origin", "main"], work)

        rollback = await submit_version_rollback(
            repo_manager,
            "test-proj",
            {
                **_git_access_auth(
                    scope_id="docs-scope", scope_path="/docs/"
                )[1],
                "agent": "rollback-agent",
            },
            {"protocol_version": PROTOCOL_VERSION, "target_commit_id": v1},
        )

        _run_git(["clone", remote, str(verify)], tmp_path)
        log = _run_git(["log", "--format=%s"], verify).decode("utf-8")

    assert rollback["status"] == "rolled-back"
    assert rollback["new_commit_id"] != v1
    assert rollback["new_commit_id"] != v2
    assert (verify / "doc.md").read_text(encoding="utf-8") == "v1\n"
    assert "rollback" in log
