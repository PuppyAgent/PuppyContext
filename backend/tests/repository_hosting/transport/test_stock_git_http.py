"""Stock Git over real HTTP + production transport/engine; memory control plane.

Every target gap first completes a normal push. A broken server/credential
therefore fails its shared fixture, rather than being hidden by an xfail.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http import AUTH, clone

pytestmark = pytest.mark.hosting_component
def test_clone_fetch_preserves_every_reachable_object_and_file_modes(hosted, tmp_path):
    git, remote, _state, _scope, _ = hosted
    git.commit({"中文 file.txt": b"\0\xffbinary\r\n", "run.sh": b"#!/bin/sh\necho yes\n"})
    git.run("update-index", "--chmod=+x", "run.sh")
    (git.path / "link").symlink_to("original.txt")
    git.run("add", "link")
    git.run("commit", "-m", "modes and symlink")
    git.run("push", "origin", "main")
    restored = clone(remote, tmp_path / "restored", git)
    assert restored.text("rev-parse", "HEAD") == git.text("rev-parse", "HEAD")
    for oid, wanted in git.objects().items():
        assert restored.objects()[oid] == wanted
    restored.run("fsck", "--full", "--strict")
    assert (restored.path / "link").is_symlink()
    assert (restored.path / "run.sh").stat().st_mode & 0o111


def test_feature_branch_and_lightweight_tag_roundtrip_without_changing_main(hosted, tmp_path):
    git, remote, _state, _scope, initial = hosted
    git.run("checkout", "-b", "feature/nested")
    feature = git.commit({"feature.txt": b"feature\n"})
    git.run("push", "origin", "feature/nested")
    git.run("tag", "v1")
    git.run("push", "origin", "v1")
    restored = clone(remote, tmp_path / "restored", git)
    assert restored.text("rev-parse", "HEAD") == initial
    assert restored.text("rev-parse", "origin/feature/nested") == feature
    assert restored.text("rev-parse", "v1") == feature
    restored.run("fsck", "--full", "--strict")


@pytest.mark.hosting_gap(
    "same-tree Git commit is acknowledged as a no-op, but clone still returns the old commit"
)
def test_empty_commit_retains_new_commit_identity_when_tree_is_unchanged(hosted, tmp_path):
    git, remote, _state, _scope, initial = hosted
    git.run("commit", "--allow-empty", "-m", "metadata-only commit")
    wanted = git.text("rev-parse", "HEAD")
    assert wanted != initial
    git.run("push", "origin", "main")
    restored = clone(remote, tmp_path / "empty-commit", git)
    assert restored.text("rev-parse", "HEAD") == wanted
    assert restored.text("rev-parse", "HEAD^{tree}") == git.text("rev-parse", initial + "^{tree}")


@pytest.mark.parametrize("hosted", [""], indirect=True)
@pytest.mark.hosting_gap(
    "same-tree pushes currently bypass publication; after that is fixed, publication must also enforce head CAS"
)
def test_two_same_tree_git_pushes_cannot_both_acknowledge_the_same_old_head(
    hosted, tmp_path, monkeypatch
):
    git, remote, state, _scope, _initial = hosted
    clients = [clone(remote, tmp_path / f"racer-{i}", git) for i in range(2)]
    for i, client in enumerate(clients):
        client.run("config", "http.extraHeader", AUTH.split("=", 1)[1])
        client.run("commit", "--allow-empty", "-m", f"concurrent empty commit {i}")
    original = state.repo.history.publish_project_update
    ready = Barrier(2)
    calls = []

    def publish(**kwargs):
        # Both requests have already validated the old Git head and staged
        # their objects. Interleave precisely at the production publish seam.
        if len(calls) < 2:
            calls.append(kwargs)
            ready.wait(timeout=15)
        return original(**kwargs)

    monkeypatch.setattr(state.repo.history, "publish_project_update", publish)
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(
            workers.map(lambda client: client.run("push", "origin", "main", check=False), clients)
        )
    assert len(calls) == 2
    assert sum(result.returncode == 0 for result in responses) == 1, [
        r.stderr.decode(errors="replace") for r in responses
    ]


def test_existing_git_history_survives_web_agent_writes_and_old_client_fetch(hosted, tmp_path):
    git, remote, state, scope, original = hosted
    original_objects = git.objects()
    for channel in ("papi", "agent"):
        asyncio.run(
            state.adapter.write_file(
                "test-proj",
                f"{channel}.txt",
                channel.encode(),
                who=f"{channel}:test",
                scope=scope,
                source_channel=channel,
                defer_projection=True,
            )
        )
    restored = clone(remote, tmp_path / "after-web-agent", git)
    for oid, raw in original_objects.items():
        assert restored.objects()[oid] == raw
    assert restored.text("show", f"{original}:original.txt") == "original"
    assert (restored.path / "papi.txt").read_bytes() == b"papi"
    assert (restored.path / "agent.txt").read_bytes() == b"agent"
    git.run("fetch", "origin")
    git.run("merge", "--ff-only", "origin/main")
    git.commit({"from-old-client": b"still works"})
    git.run("push", "origin", "main")
    final = clone(remote, tmp_path / "final", git)
    assert (final.path / "from-old-client").read_bytes() == b"still works"
    assert (final.path / "papi.txt").read_bytes() == b"papi"
    final.run("fsck", "--full", "--strict")


def test_read_only_credential_can_fetch_but_cannot_change_saved_data(hosted, tmp_path, monkeypatch):
    from src.version_engine.entrypoints.git import auth

    git, remote, _, _, original = hosted
    resolve = auth.AccessCredentialRepository.resolve_git_runtime_credential

    def readonly(self, token):
        row = resolve(self, token)
        return {**row, "effective_mode": "r"} if row else row

    monkeypatch.setattr(auth.AccessCredentialRepository, "resolve_git_runtime_credential", readonly)
    git.commit({"forbidden": b"do not publish"})
    result = git.run("push", "origin", "main", check=False)
    assert result.returncode != 0
    assert b"read-only" in result.stderr
    restored = clone(remote, tmp_path / "readonly", git)
    assert restored.text("rev-parse", "HEAD") == original
    assert not (restored.path / "forbidden").exists()


@pytest.mark.parametrize("body", [b"zzzz", b"0003", b"0030truncated", b"0008oops0000"])
def test_malformed_push_never_changes_existing_head(hosted, body, tmp_path):
    git, remote, _, _, original = hosted
    response = httpx.post(
        remote + "/git-receive-pack",
        content=body,
        auth=("git", "git_secret"),
        headers={"Content-Type": "application/x-git-receive-pack-request"},
        timeout=10,
    )
    assert response.status_code == 400, response.text
    restored = clone(remote, tmp_path / "after-rejection", git)
    assert restored.text("rev-parse", "HEAD") == original
    restored.run("fsck", "--full", "--strict")


@pytest.mark.parametrize("rewrite", ["squash", "rebase", "cherry-pick", "revert", "amend"])
def test_locally_created_history_operations_can_be_hosted(hosted, tmp_path, rewrite):
    git, remote, _, _, initial = hosted
    git.run("checkout", "-b", "work")
    one = git.commit({"one": b"one"}, "first")
    git.commit({"two": b"two"}, "second")
    if rewrite == "squash":
        git.run("reset", "--soft", initial)
        git.run("commit", "-m", "squashed")
    elif rewrite == "rebase":
        git.run("checkout", "main")
        git.commit({"unrelated": b"base changed"})
        git.run("checkout", "work")
        git.run("rebase", "main")
    elif rewrite == "cherry-pick":
        git.run("checkout", "main")
        git.run("cherry-pick", one)
    elif rewrite == "revert":
        git.run("revert", "--no-edit", "HEAD")
    else:
        git.run("commit", "--amend", "-m", "amended before push")
    wanted = git.text("rev-parse", "HEAD")
    git.run("push", "origin", "HEAD:refs/heads/main")
    restored = clone(remote, tmp_path / "rewritten", git)
    assert restored.text("rev-parse", "HEAD") == wanted
    assert (
        restored.run("diff-tree", "-r", "--root", "HEAD").stdout
        == git.run("diff-tree", "-r", "--root", "HEAD").stdout
    )
    restored.run("fsck", "--full", "--strict")


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            "delete",
            marks=pytest.mark.hosting_gap("receive-pack explicitly refuses deleting references"),
        ),
        pytest.param(
            "force",
            marks=pytest.mark.hosting_gap(
                "main enforces fast-forward even with a valid force lease"
            ),
        ),
        pytest.param(
            "merge",
            marks=pytest.mark.hosting_gap("receive-pack rejects commits with multiple parents"),
        ),
        pytest.param(
            "annotated-tag",
            marks=pytest.mark.hosting_gap(
                "tag object rejected: pushed reference must point directly at a commit"
            ),
        ),
        pytest.param("notes", marks=pytest.mark.hosting_gap("ref whitelist excludes refs/notes")),
        pytest.param(
            "atomic", marks=pytest.mark.hosting_gap("multi-reference receive command unsupported")
        ),
        "shallow",
        pytest.param(
            "blob-tag",
            marks=pytest.mark.hosting_gap(
                "tags pointing to blobs rejected by commit-only validation"
            ),
        ),
    ],
)
@pytest.mark.parametrize("hosted", [""], indirect=True, ids=["project"])
def test_bare_git_target_operations(hosted, tmp_path, operation):
    git, remote, _, _, initial = hosted
    if operation == "delete":
        git.run("push", "origin", "main:refs/heads/delete-me")
        git.run("push", "origin", ":refs/heads/delete-me")
        assert b"delete-me" not in git.run("ls-remote", "origin").stdout
    elif operation == "force":
        git.commit({"later": b"later"})
        git.run("push", "origin", "main")
        git.run("reset", "--hard", initial)
        git.run("push", "--force-with-lease", "origin", "main")
        assert clone(remote, tmp_path / "forced", git).text("rev-parse", "HEAD") == initial
    elif operation == "merge":
        git.run("checkout", "-b", "topic")
        git.commit({"topic": b"topic"})
        git.run("checkout", "main")
        git.commit({"main": b"main"})
        git.run("merge", "--no-ff", "topic", "-m", "merge")
        git.run("push", "origin", "main")
        restored = clone(remote, tmp_path / "merged", git)
        assert restored.text("rev-list", "--parents", "-n1", "HEAD") == git.text(
            "rev-list", "--parents", "-n1", "HEAD"
        )
    elif operation == "annotated-tag":
        git.run("tag", "-a", "annotated", "-m", "tag body")
        git.run("push", "origin", "annotated")
        restored = clone(remote, tmp_path / "tagged", git)
        assert (
            restored.run("cat-file", "tag", "annotated").stdout
            == git.run("cat-file", "tag", "annotated").stdout
        )
    elif operation == "notes":
        git.run("notes", "add", "-m", "review metadata")
        git.run("push", "origin", "refs/notes/commits")
        restored = clone(remote, tmp_path / "notes", git)
        restored.run("-c", AUTH, "fetch", "origin", "refs/notes/commits:refs/notes/commits")
        assert restored.text("notes", "show") == "review metadata"
    elif operation == "atomic":
        git.run("push", "--atomic", "origin", "main:refs/heads/one", "main:refs/heads/two")
        advertised = git.run("ls-remote", "origin").stdout
        assert b"refs/heads/one" in advertised and b"refs/heads/two" in advertised
    elif operation == "shallow":
        git.commit({"new": b"new"})
        git.run("push", "origin", "main")
        git.run("-c", AUTH, "clone", "--depth=1", remote, tmp_path / "shallow")
        shallow = Git(tmp_path / "shallow")
        assert shallow.text("rev-parse", "--is-shallow-repository") == "true"
        shallow.run("-c", AUTH, "fetch", "--unshallow", "origin")
        assert shallow.text("rev-parse", "--is-shallow-repository") == "false"
    else:
        blob = git.text("rev-parse", "HEAD:original.txt")
        git.run("tag", "blob", blob)
        git.run("push", "origin", "blob")
        restored = clone(remote, tmp_path / "blob-tag", git)
        assert restored.text("cat-file", "-t", "blob") == "blob"
