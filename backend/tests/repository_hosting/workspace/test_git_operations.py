import pytest

from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_native


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_full_history_mirror_bundle_and_fsck(tmp_path, format):
    repo = Git.init(tmp_path / "work", format=format)
    repo.commit({"a": b"initial\n"})
    repo.run("switch", "-c", "feature")
    repo.commit({"feature": b"yes\n"}, "feature")
    repo.run("switch", "main")
    repo.commit({"main": b"yes\n"}, "main")
    repo.run("merge", "--no-ff", "feature", "-m", "merge")
    repo.run("tag", "-a", "v1", "-m", "annotated")
    repo.run("notes", "add", "-m", "note")
    bare = Git.init(tmp_path / "bare", bare=True, format=format)
    repo.run("push", "--mirror", str(bare.path))
    assert bare.refs() == repo.refs()
    assert bare.objects() == repo.objects()
    bare.run("fsck", "--strict", "--no-dangling")
    bundle = tmp_path / "backup.bundle"
    bare.run("bundle", "create", str(bundle), "--all")
    bare.run("bundle", "verify", str(bundle))
    restored = Git.init(tmp_path / "restored", bare=True, format=format)
    restored.run("fetch", str(bundle), "+refs/*:refs/*")
    assert restored.refs() == bare.refs()
    assert restored.objects() == bare.objects()


@pytest.mark.parametrize("operation", ["merge", "rebase", "cherry-pick", "revert", "am"])
def test_conflict_abort_restores_exact_head_and_files(git_repo, operation):
    git_repo.commit({"f": b"base\n"}, "base")
    git_repo.run("switch", "-c", "other")
    theirs = git_repo.commit({"f": b"theirs\n"}, "theirs")
    patch = git_repo.run("format-patch", "-1", "--stdout").stdout
    git_repo.run("switch", "main")
    ours = git_repo.commit({"f": b"ours\n"}, "ours")
    command = {
        "merge": ("merge", "other"),
        "rebase": ("rebase", "other"),
        "cherry-pick": ("cherry-pick", theirs),
        "revert": ("revert", "--no-edit", theirs),
        "am": ("am", "--3way"),
    }[operation]
    result = git_repo.run(*command, input=patch if operation == "am" else None, check=False)
    assert result.returncode != 0
    assert git_repo.text("ls-files", "--unmerged")
    git_repo.run(operation, "--abort")
    assert git_repo.text("rev-parse", "HEAD") == ours
    assert (git_repo.path / "f").read_bytes() == b"ours\n"
    assert git_repo.text("status", "--porcelain") == ""


@pytest.mark.parametrize("operation", ["merge", "rebase", "cherry-pick"])
def test_resolve_and_continue_then_push_to_bare(git_repo, tmp_path, operation):
    git_repo.commit({"f": b"base\n"})
    git_repo.run("switch", "-c", "other")
    theirs = git_repo.commit({"f": b"theirs\n"}, "theirs")
    git_repo.run("switch", "main")
    git_repo.commit({"f": b"ours\n"}, "ours")
    target = theirs if operation == "cherry-pick" else "other"
    assert git_repo.run(operation, target, check=False).returncode != 0
    (git_repo.path / "f").write_bytes(b"both\n")
    git_repo.run("add", "f")
    git_repo.run(operation, "--continue")
    remote = Git.init(tmp_path / "remote", bare=True)
    git_repo.run("push", str(remote.path), "HEAD:refs/heads/main")
    assert remote.text("rev-parse", "main") == git_repo.text("rev-parse", "HEAD")
    assert remote.run("show", "main:f").stdout == b"both\n"


@pytest.mark.parametrize("mode", ["--soft", "--mixed", "--hard"])
def test_reset_index_and_worktree_contract(git_repo, mode):
    base = git_repo.commit({"f": b"base"})
    git_repo.commit({"f": b"next"}, "next")
    git_repo.run("reset", mode, base)
    assert git_repo.text("rev-parse", "HEAD") == base
    assert git_repo.run("show", ":f").stdout == (b"next" if mode == "--soft" else b"base")
    assert (git_repo.path / "f").read_bytes() == (b"base" if mode == "--hard" else b"next")


def test_squash_has_one_parent_and_keeps_both_changes(git_repo):
    base = git_repo.commit({"base": b"base"})
    git_repo.run("switch", "-c", "feature")
    git_repo.commit({"a": b"A"}, "A")
    git_repo.commit({"b": b"B"}, "B")
    git_repo.run("switch", "main")
    git_repo.run("merge", "--squash", "feature")
    git_repo.run("commit", "-m", "squashed")
    assert git_repo.text("show", "-s", "--format=%P") == base
    assert git_repo.run("show", "HEAD:a").stdout == b"A"
    assert git_repo.run("show", "HEAD:b").stdout == b"B"


def test_stash_untracked_restore_and_separate_worktree(git_repo, tmp_path):
    git_repo.commit({"f": b"base"})
    (git_repo.path / "f").write_bytes(b"dirty")
    (git_repo.path / "untracked").write_bytes(b"keep")
    git_repo.run("stash", "push", "--include-untracked")
    assert git_repo.text("status", "--porcelain") == ""
    git_repo.run("worktree", "add", "-b", "feature", str(tmp_path / "other"))
    other = Git(tmp_path / "other")
    other.commit({"feature": b"separate"}, "other")
    assert not (git_repo.path / "feature").exists()
    git_repo.run("stash", "pop")
    assert (git_repo.path / "f").read_bytes() == b"dirty"
    assert (git_repo.path / "untracked").read_bytes() == b"keep"
