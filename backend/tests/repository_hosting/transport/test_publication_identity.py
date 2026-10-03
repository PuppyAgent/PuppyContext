"""Empty commits are version facts; cache leases are not publication locks."""

import asyncio
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.version_engine.adapters.git import object_quarantine
from src.version_engine.adapters.git.submission import submit_git_tree
from src.version_engine.write_engine.engine import NonFastForwardSubmissionError
from tests.repository_hosting.harness.conformance import remote_snapshot
from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("copy_fallback", [False, True], ids=["hardlink", "copy"])
def test_receive_snapshot_releases_cache_lease_and_survives_pruning(
    hosted, tmp_path, monkeypatch, copy_fallback,
):
    client, _, state, scope, initial = hosted
    if copy_fallback:
        def unsupported(*args, **kwargs):
            raise OSError("injected cross-device hard-link failure")
        monkeypatch.setattr(object_quarantine.os, "link", unsupported)
    with ThreadPoolExecutor(max_workers=1) as pool, object_quarantine.quarantine_bare_repo(
        state.repo, scope, follow_history=True, detach_cache=True,
    ) as bare:
        assert not (bare / "objects/info/alternates").exists()
        # A second request can acquire the same cache lease before the first
        # request completes publication. Exiting the quarantine first also
        # guarantees a failing assertion cannot strand a waiting worker.
        future = pool.submit(object_quarantine.warm_transport_bare_repo, state.repo, scope)
        assert future.result(timeout=5) == initial
        assert state.git_cache_root.is_relative_to(tmp_path)
        shutil.rmtree(state.git_cache_root)
        detached = Git(bare)
        detached.run("fsck", "--full", "--strict")
        assert detached.text("rev-parse", "HEAD") == initial
        assert detached.objects() == client.objects()
    assert not bare.exists()


def test_multiple_metadata_only_commits_survive_cold_read_and_retry(hosted, tmp_path, monkeypatch):
    client, remote, state, _, initial = hosted
    publish = state.repo.history.publish_project_update
    accepted = []

    def record(**kwargs):
        result = publish(**kwargs)
        accepted.append((kwargs, result))
        return result

    monkeypatch.setattr(state.repo.history, "publish_project_update", record)
    for index in range(3):
        client.run("commit", "--allow-empty", "-m", f"metadata {index}")
        client.run("push", "origin", "main")
    wanted = client.text("rev-parse", "HEAD")
    assert len(accepted) == 3 and all(result[0] for _, result in accepted)
    assert all(row["expected_scope_head_commit_id"] is not None for row, _ in accepted)
    assert client.text("rev-parse", "HEAD^{tree}") == client.text("rev-parse", initial + "^{tree}")
    assert state.git_cache_root.is_relative_to(tmp_path)
    shutil.rmtree(state.git_cache_root)
    restored = remote_snapshot(client, remote, tmp_path / "metadata.git")
    assert restored.refs == f"refs/heads/main {wanted}\n".encode()
    assert restored.objects == client.objects()
    client.run("push", "origin", "main")
    assert len(accepted) == 3


def test_rejected_empty_commit_is_not_acknowledged_and_retry_preserves_identity(
    hosted, tmp_path, monkeypatch,
):
    client, remote, state, _, initial = hosted
    client.run("commit", "--allow-empty", "-m", "retry this exact identity")
    wanted = client.text("rev-parse", "HEAD")

    def fail(**kwargs):
        raise RuntimeError("injected publish failure")

    with monkeypatch.context() as fault:
        fault.setattr(state.repo.history, "publish_project_update", fail)
        assert client.run("push", "origin", "main", check=False).returncode != 0
    assert client.text("rev-parse", "HEAD") == wanted
    assert state.git_cache_root.is_relative_to(tmp_path)
    shutil.rmtree(state.git_cache_root)
    before = remote_snapshot(client, remote, tmp_path / "before-retry.git")
    assert before.refs == f"refs/heads/main {initial}\n".encode()
    client.run("push", "origin", "main")
    shutil.rmtree(state.git_cache_root)
    after = remote_snapshot(client, remote, tmp_path / "after-retry.git")
    assert after.refs == f"refs/heads/main {wanted}\n".encode()
    assert after.objects == client.objects()


@pytest.mark.parametrize("hosted", ["docs"], indirect=True)
def test_stale_scope_view_alias_is_rejected_before_first_publication(hosted, monkeypatch):
    client, _, state, scope, initial = hosted
    asyncio.run(state.adapter.write_file(
        "test-proj", "winner.txt", b"must survive", "papi:winner",
        scope=scope, defer_projection=True,
    ))
    before = state.repo.get_scope_head_commit_id(scope)
    assert before != initial
    calls = []
    publish = state.repo.history.publish_project_update

    def record(**kwargs):
        calls.append(kwargs)
        return publish(**kwargs)

    monkeypatch.setattr(state.repo.history, "publish_project_update", record)
    with pytest.raises(NonFastForwardSubmissionError):
        asyncio.run(submit_git_tree(
            state.manager, project_id="test-proj", scope_path=scope, actor="git:stale",
            base_commit_id=initial, client_commit_id=initial,
            proposed_tree_id=client.text("rev-parse", initial + "^{tree}"),
            message="stale advertised alias", defer_projection=True,
            audit_detail={"git_visible_old_commit_id": initial},
        ))
    assert calls == []
    assert state.repo.get_scope_head_commit_id(scope) == before
