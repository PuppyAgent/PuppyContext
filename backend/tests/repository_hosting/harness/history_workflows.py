"""Executable history/workspace recipes, run against both native bare Git and HTTP."""

from functools import partial

from tests.repository_hosting.harness.conformance import Workflow, reachable_objects
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http import clone_client

DAG_GAP = "Project receive-pack rejects multi-parent commits; preserve the original DAG"
REWRITE_GAP = "Project main rejects explicit history rewrites"


def push_main(git):
    git.run("push", "origin", "HEAD:refs/heads/main")


def commit_index(git):
    (git.path / "staged.txt").write_bytes(b"staged\n")
    (git.path / "discard.txt").write_bytes(b"untracked\n")
    git.run("add", "staged.txt")
    assert git.run("show", ":staged.txt").stdout == b"staged\n"
    git.run("restore", "--staged", "staged.txt")
    assert "?? staged.txt" in git.text("status", "--porcelain")
    git.run("add", "staged.txt")
    (git.path / "staged.txt").write_bytes(b"unstaged\n")
    git.run("restore", "staged.txt")
    assert (git.path / "staged.txt").read_bytes() == b"staged\n"
    git.run("clean", "-fd", "--", "discard.txt")
    git.run("mv", "original.txt", "renamed.txt")
    git.run("commit", "-m", "index and rename")
    git.run("rm", "staged.txt")
    git.run("commit", "-m", "versioned deletion")
    push_main(git)


def merge_history(git, *, mode):
    git.run("switch", "-c", "topic")
    first = git.commit({"topic.txt": b"topic\n"}, "topic")
    git.run("switch", "main")
    if mode in {"no-ff", "octopus"}:
        git.commit({"main.txt": b"main\n"}, "main diverges")
    if mode == "octopus":
        git.run("switch", "-c", "second", first + "^")
        git.commit({"second.txt": b"second\n"}, "second topic")
        git.run("switch", "main")
        git.run("merge", "--no-ff", "topic", "second", "-m", "octopus")
        assert len(git.text("show", "-s", "--format=%P").split()) == 3
    elif mode == "squash":
        git.run("merge", "--squash", "topic")
        git.run("commit", "-m", "squashed topic")
        assert len(git.text("show", "-s", "--format=%P").split()) == 1
    else:
        git.run("merge", "--" + mode, "topic", "-m", "merged topic")
    push_main(git)


def cherry_pick(git, *, no_commit):
    base = git.text("rev-parse", "HEAD")
    git.run("switch", "-c", "topic")
    git.commit({"one.txt": b"one\n"}, "one")
    git.commit({"two.txt": b"two\n"}, "two")
    git.run("switch", "main")
    git.commit({"main.txt": b"main\n"}, "independent base")
    git.run("cherry-pick", *(["--no-commit"] if no_commit else []), base + "..topic")
    if no_commit:
        git.run("commit", "-m", "combined picks")
    assert (git.path / "one.txt").exists() and (git.path / "two.txt").exists()
    push_main(git)


def revert_range(git):
    base = git.text("rev-parse", "HEAD")
    git.commit({"one.txt": b"one\n"}, "one")
    last = git.commit({"two.txt": b"two\n"}, "two")
    push_main(git)
    git.run("revert", "--no-edit", base + ".." + last)
    assert not (git.path / "one.txt").exists() and not (git.path / "two.txt").exists()
    assert git.text("rev-list", "--count", base + "..HEAD") == "4"
    push_main(git)


def patch_workflow(git, *, mode):
    if mode == "am":
        base = git.text("rev-parse", "HEAD")
        git.run("switch", "-c", "topic")
        git.commit({"patch.txt": b"mail patch\n"}, "mail one")
        git.commit({"binary.dat": b"\x00\xff\n"}, "mail two")
        patch = git.run("format-patch", "--stdout", base + "..HEAD").stdout
        git.run("switch", "main")
        git.commit({"main.txt": b"independent\n"}, "advance base")
        git.run("am", "--3way", input=patch)
    else:
        (git.path / "original.txt").write_bytes(b"changed\x00\xff\n")
        patch = git.run("diff", "--binary").stdout
        git.run("restore", "original.txt")
        git.run("apply", "--check", input=patch)
        git.run("apply", "--index", input=patch)
        git.run("commit", "-m", "applied binary patch")
    push_main(git)


def rebase_history(git, *, mode):
    git.run("switch", "-c", "topic")
    first = git.commit({"one.txt": b"one\n"}, "one")
    if mode == "autosquash":
        (git.path / "one.txt").write_bytes(b"one fixed\n")
        git.run("add", "one.txt")
        git.run("commit", "--fixup", first)
    else:
        git.commit({"two.txt": b"two\n"}, "two")
    git.run("switch", "main")
    git.commit({"main.txt": b"new base\n"}, "new base")
    git.run("switch", "topic")
    if mode == "onto":
        git.run("rebase", "--onto", "main", first)
        assert not (git.path / "one.txt").exists()
    elif mode == "autosquash":
        git.run("rebase", "--interactive", "--autosquash", "main")
        assert git.text("rev-list", "--count", "main..HEAD") == "1"
        assert (git.path / "one.txt").read_bytes() == b"one fixed\n"
    else:
        git.run("rebase", "main")
    push_main(git)


def conflict_workflow(git, *, operation, finish):
    git.run("switch", "-c", "other")
    theirs = git.commit({"original.txt": b"theirs\n"}, "conflicting topic")
    patch = git.run("format-patch", "-1", "--stdout").stdout
    git.run("switch", "main")
    ours = git.commit({"original.txt": b"ours\n"}, "conflicting main")
    args = {
        "merge": ("merge", "other"), "rebase": ("rebase", "other"),
        "cherry-pick": ("cherry-pick", theirs),
        "revert": ("revert", "--no-edit", theirs), "am": ("am", "--3way"),
    }[operation]
    conflict = git.run(*args, input=patch if operation == "am" else None, check=False)
    assert conflict.returncode != 0 and git.text("ls-files", "--unmerged")
    if finish == "continue":
        (git.path / "original.txt").write_bytes(b"resolved both\n")
        git.run("add", "original.txt")
        git.run(operation, "--continue")
        assert (git.path / "original.txt").read_bytes() == b"resolved both\n"
    elif finish == "abort":
        git.run(operation, "--abort")
        assert git.text("rev-parse", "HEAD") == ours
        assert (git.path / "original.txt").read_bytes() == b"ours\n"
    else:
        git.run(operation, "--skip")
        expected = b"theirs\n" if operation == "rebase" else b"ours\n"
        assert (git.path / "original.txt").read_bytes() == expected
    assert git.text("status", "--porcelain") == ""
    git.commit({"after.txt": b"sequencer finished\n"}, "after sequencer")
    push_main(git)


def reset_workflow(git, *, mode):
    base = git.text("rev-parse", "HEAD")
    lost = git.commit({"new.txt": b"recover me\n"}, "before reset")
    git.run("reset", "--" + mode, base)
    if mode == "hard":
        assert not (git.path / "new.txt").exists()
        assert lost in git.text("reflog", "show", "--format=%H").splitlines()
        git.run("branch", "recovered", lost)
        git.run("push", "origin", "recovered")
    else:
        assert (git.path / "new.txt").read_bytes() == b"recover me\n"
        indexed = git.run("show", ":new.txt", check=False)
        assert (indexed.returncode == 0) == (mode == "soft")
        git.run("add", "new.txt")
        git.run("commit", "-m", "recommit after reset")
        push_main(git)


def workspace_workflow(git, *, mode):
    if mode == "stash":
        (git.path / "original.txt").write_bytes(b"dirty tracked\n")
        (git.path / "untracked.txt").write_bytes(b"private\n")
        git.run("stash", "push", "--include-untracked")
        assert git.text("status", "--porcelain") == ""
        git.commit({"intervening.txt": b"independent\n"}, "while stashed")
        git.run("stash", "pop")
        assert (git.path / "original.txt").read_bytes() == b"dirty tracked\n"
        assert (git.path / "untracked.txt").read_bytes() == b"private\n"
        git.commit({}, "restored working state")
        push_main(git)
    elif mode == "worktree":
        other_path = git.path.parent / (git.path.name + "-linked")
        git.run("worktree", "add", "-b", "linked", other_path)
        other = Git(other_path)
        other.commit({"linked.txt": b"linked\n"}, "linked workspace")
        assert not (git.path / "linked.txt").exists()
        other.run("push", "origin", "linked")
        git.run("worktree", "remove", other_path)
    elif mode == "sparse":
        git.commit({"docs/a.txt": b"docs\n", "outside/b.txt": b"retain\n"}, "sparse seed")
        push_main(git)
        git.run("sparse-checkout", "init", "--cone")
        git.run("sparse-checkout", "set", "docs")
        assert not (git.path / "outside" / "b.txt").exists()
        git.commit({"docs/a.txt": b"edited\n"}, "sparse edit")
        assert git.run("show", "HEAD:outside/b.txt").stdout == b"retain\n"
        push_main(git)
    else:
        git.run("switch", "--detach")
        git.commit({"detached.txt": b"explicit destination\n"}, "detached commit")
        git.run("push", "origin", "HEAD:refs/heads/detached-result")


def pull_workflow(git, *, mode):
    remote = git.text("remote", "get-url", "origin")
    other = clone_client(remote, git.path.parent / (git.path.name + "-peer"), git)
    other.commit({"peer.txt": b"remote work\n"}, "peer")
    push_main(other)
    if mode == "ff-only":
        git.run("pull", "--ff-only", "origin", "main")
    else:
        local = git.commit({"local.txt": b"local work\n"}, "local")
        if mode == "diverged-ff-only":
            assert git.run("pull", "--ff-only", "origin", "main", check=False).returncode != 0
            assert git.text("rev-parse", "HEAD") == local
            assert (git.path / "local.txt").read_bytes() == b"local work\n"
        if mode == "merge":
            # Git's default merge message embeds the remote URL (file:// vs
            # HTTP), legitimately changing OIDs. Fix the input message before
            # both recipes; retain exact raw-object/OID comparisons afterward.
            git.run("config", "branch.main.mergeOptions", "--message=pull-merge-conformance")
        git.run("pull", "--no-rebase" if mode == "merge" else "--rebase", "origin", "main")
    git.commit({"after-pull.txt": b"ready\n"}, "after pull")
    push_main(git)


def submodule_pointer(git):
    external = Git.init(git.path.parent / (git.path.name + "-external"))
    oid = external.commit({"external.txt": b"not in hosted repository\n"}, "external root")
    (git.path / ".gitmodules").write_bytes(
        b'[submodule "vendor/sdk"]\n\tpath = vendor/sdk\n\turl = https://example.invalid/sdk.git\n',
    )
    git.run("add", ".gitmodules")
    git.run("update-index", "--add", "--cacheinfo", f"160000,{oid},vendor/sdk")
    git.run("commit", "-m", "external gitlink")
    assert git.run("cat-file", "-e", oid, check=False).returncode != 0
    push_main(git)


def raw_commit(git):
    parent = git.text("rev-parse", "HEAD")
    git.commit({"raw.txt": b"raw headers\n"}, "temporary tree builder")
    tree = git.text("rev-parse", "HEAD^{tree}")
    # Opaque signature bytes test preservation, NOT signature authenticity.
    body = (
        f"tree {tree}\nparent {parent}\n".encode()
        + b"author Raw <raw@example.test> 1700000000 +0530\n"
        + b"committer Other <other@example.test> 1700000100 -0400\n"
        + b"encoding ISO-8859-1\ngpgsig opaque-test-not-a-valid-signature\n continuation\n"
        + b"x-custom retained\n\nraw message \xff\r\n"
    )
    oid = git.run("hash-object", "-t", "commit", "-w", "--stdin", input=body).stdout.strip().decode()
    git.run("update-ref", "refs/heads/main", oid)
    push_main(git)


def client_export(git, *, mode):
    git.commit({"export.txt": b"exportable\x00\xff"}, "export seed")
    push_main(git)
    git.run("tag", "export-tag")
    git.run("push", "origin", "export-tag")
    remote = git.text("remote", "get-url", "origin")
    mirror = clone_client(remote, git.path.parent / (git.path.name + "-mirror"), git, "--mirror")
    expected = mirror.refs(), reachable_objects(mirror)
    restored = Git.init(git.path.parent / (git.path.name + "-restored"), bare=True)
    if mode == "bundle":
        bundle = git.path.parent / (git.path.name + ".bundle")
        mirror.run("bundle", "create", bundle, "--all")
        mirror.run("bundle", "verify", bundle)
        restored.run("fetch", bundle, "+refs/*:refs/*")
    else:
        stream = mirror.run("fast-export", "--all").stdout
        restored.run("fast-import", "--quiet", input=stream)
    assert (restored.refs(), reachable_objects(restored)) == expected
    restored.run("fsck", "--full", "--strict")


HISTORY_WORKFLOWS = [
    Workflow("index-restore-move-delete", ("G31", "G59"), ("add", "restore", "status", "clean", "mv", "rm", "commit"), commit_index),
    *[Workflow("merge-" + mode, {"ff-only": ("G32",), "squash": ("G34",), "no-ff": ("G09", "G33"), "octopus": ("G09", "G33")}[mode], ("merge", "commit"), partial(merge_history, mode=mode), DAG_GAP if mode in {"no-ff", "octopus"} else "") for mode in ("ff-only", "squash", "no-ff", "octopus")],
    *[Workflow("cherry-pick-" + mode, ("G37",), ("cherry-pick", "commit"), partial(cherry_pick, no_commit=mode == "no-commit")) for mode in ("range", "no-commit")],
    Workflow("revert-range", ("G37", "G38"), ("revert", "rev-list"), revert_range),
    *[Workflow("patch-" + mode, ("G37",), ("format-patch", "am") if mode == "am" else ("diff", "apply"), partial(patch_workflow, mode=mode)) for mode in ("am", "apply")],
    *[Workflow("rebase-" + mode, ("G35",), ("rebase", "commit"), partial(rebase_history, mode=mode)) for mode in ("normal", "onto", "autosquash")],
    *[Workflow(f"{op}-conflict-{finish}", ({"merge": "G33", "rebase": "G35"}.get(op, "G37"), "G43"), (op, "add", "status", "ls-files"), partial(conflict_workflow, operation=op, finish=finish)) for op in ("merge", "rebase", "cherry-pick", "revert", "am") for finish in ("abort", "continue", "skip") if not (op == "merge" and finish == "skip")],
    *[Workflow("reset-" + mode, ("G30", "G38") if mode == "hard" else ("G38", "G59"), ("reset", "reflog", "branch") if mode == "hard" else ("reset", "add", "commit"), partial(reset_workflow, mode=mode)) for mode in ("soft", "mixed", "hard")],
    *[Workflow("workspace-" + mode, ("G61",) if mode == "stash" else ("G60",), {"stash": ("stash", "status", "commit"), "worktree": ("worktree", "commit"), "sparse": ("sparse-checkout", "commit"), "detached": ("switch", "commit")}[mode], partial(workspace_workflow, mode=mode)) for mode in ("stash", "worktree", "sparse", "detached")],
    *[Workflow("pull-" + mode, ({"merge": "G33", "ff-only": "G32"}.get(mode, "G35"), "G44"), ("pull", "commit", "push"), partial(pull_workflow, mode=mode), DAG_GAP if mode == "merge" else "") for mode in ("ff-only", "rebase", "merge", "diverged-ff-only")],
    Workflow("external-gitlink", ("G05", "G14"), ("update-index", "commit", "cat-file"), submodule_pointer),
    Workflow("raw-commit-headers", ("G06", "G13"), ("hash-object", "update-ref", "cat-file"), raw_commit),
    *[Workflow("client-export-" + mode, ("G52", "G54"), ("bundle",) if mode == "bundle" else ("fast-export", "fast-import"), partial(client_export, mode=mode)) for mode in ("bundle", "fast-import")],
]
