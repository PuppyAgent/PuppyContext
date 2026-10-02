"""User refs and rewrite recipes. No direct mutation of the hosted control plane."""

from functools import partial

from tests.repository_hosting.harness.conformance import Workflow
from tests.repository_hosting.harness.http import clone_client

DELETE_GAP = "receive-pack rejects ref deletion"
REWRITE_GAP = "Project main rejects explicit non-fast-forward rewrites"
MULTI_GAP = "receive-pack does not support multi-command/atomic publication"
TYPED_GAP = "receive-pack requires a commit target rather than a generic Git object"
GENERIC_GAP = "receive-pack ref whitelist excludes notes/replace/custom refs"


def branch_workflow(git, *, mode):
    if mode == "orphan":
        git.run("switch", "--orphan", "orphan")
        git.commit({"orphan.txt": b"independent history\n"}, "orphan root")
        assert git.text("show", "-s", "--format=%P") == ""
        git.run("push", "origin", "orphan")
        return
    git.run("branch", "source")
    git.run("push", "origin", "source")
    if mode == "copy":
        git.run("branch", "--copy", "source", "copied")
        git.run("push", "origin", "copied")
    elif mode == "rename":
        git.run("branch", "--move", "source", "renamed")
        git.run("push", "--atomic", "origin", "renamed", ":refs/heads/source")
    elif mode == "delete":
        git.run("push", "origin", "--delete", "source")
    elif mode == "prune":
        peer = clone_client(
            git.text("remote", "get-url", "origin"),
            git.path.parent / (git.path.name + "-prune"), git,
        )
        assert peer.text("rev-parse", "refs/remotes/origin/source")
        git.run("push", "origin", "--delete", "source")
        peer.run("fetch", "--prune", "origin")
        assert peer.run("show-ref", "--verify", "refs/remotes/origin/source", check=False).returncode != 0
    else:
        git.run("switch", "source")
        git.commit({"source.txt": b"one\n"}, "branch one")
        git.run("push", "origin", "source")
        git.commit({"source.txt": b"two\n"}, "branch two")
        git.run("push", "origin", "source")


def rewrite_workflow(git, *, mode):
    base = git.text("rev-parse", "HEAD")
    published = git.commit({"published.txt": b"old history\n"}, "published")
    git.run("push", "origin", "main")
    git.run("reset", "--hard", base)
    git.commit({"replacement.txt": b"replacement\n"}, "replacement")
    if mode == "stale-lease":
        rejected = git.run("push", f"--force-with-lease=refs/heads/main:{base}", "origin", "main", check=False)
        assert rejected.returncode != 0 and b"stale info" in rejected.stderr
        assert published.encode() in git.run("ls-remote", "origin", "refs/heads/main").stdout
    elif mode == "lease":
        git.run("push", f"--force-with-lease=refs/heads/main:{published}", "origin", "main")
    elif mode == "plus-refspec":
        git.run("push", "origin", "+main:refs/heads/main")
    else:
        git.run("push", "--force", "origin", "main")


def tag_workflow(git, *, mode):
    git.run("tag", "release")
    git.run("push", "origin", "release")
    if mode == "delete":
        git.run("push", "origin", ":refs/tags/release")
    elif mode == "overwrite":
        git.commit({"new.txt": b"new tag target\n"}, "new target")
        git.run("tag", "--force", "release")
        git.run("push", "--force", "origin", "refs/tags/release")
    elif mode in {"blob", "tree"}:
        oid = git.text("rev-parse", "HEAD:original.txt" if mode == "blob" else "HEAD^{tree}")
        git.run("tag", "object-tag", oid)
        git.run("push", "origin", "refs/tags/object-tag")
    else:
        git.run("tag", "-a", "annotated", "-m", "annotated message")
        if mode == "nested":
            git.run("tag", "-a", "outer", "annotated", "-m", "tag of tag")
            git.run("push", "origin", "refs/tags/outer")
        else:
            git.run("push", "origin", "refs/tags/annotated")


def generic_ref(git, *, mode):
    original = git.text("rev-parse", "HEAD")
    if mode == "notes":
        git.run("notes", "add", "-m", "first note", original)
        git.run("notes", "append", "-m", "second note", original)
        ref = "refs/notes/commits"
    elif mode == "replace":
        other = git.commit({"replacement.txt": b"replacement graph\n"}, "replacement")
        git.run("replace", original, other)
        ref = "refs/replace/" + original
    else:
        ref = "refs/checkpoints/session-1"
        git.run("update-ref", ref, original)
    git.run("--no-replace-objects", "push", "origin", ref + ":" + ref)


def multi_ref(git, *, mode):
    git.run("branch", "one")
    git.run("branch", "two")
    if mode == "all":
        git.run("push", "--all", "origin")
    elif mode == "tags":
        git.run("tag", "one-tag")
        git.run("tag", "two-tag")
        git.run("push", "--tags", "origin")
    elif mode == "atomic":
        git.run("push", "--atomic", "origin", "one", "two")
    else:
        git.run("push", "--mirror", "origin")


def mixed_batch(git, *, atomic):
    base = git.text("rev-parse", "HEAD")
    git.run("switch", "-c", "blocked")
    old = git.commit({"old.txt": b"published\n"}, "blocked old")
    git.run("push", "origin", "blocked")
    git.run("reset", "--hard", base)
    git.commit({"new.txt": b"non fast forward\n"}, "blocked new")
    git.run("branch", "allowed")
    rejected = git.run("push", *(["--atomic"] if atomic else []), "origin", "blocked", "allowed", check=False)
    # An absent capability or authentication failure is NOT rollback evidence.
    assert rejected.returncode != 0 and b"non-fast-forward" in rejected.stderr, rejected.stderr.decode(errors="replace")
    refs = git.run("ls-remote", "--refs", "origin").stdout
    assert old.encode() + b"\trefs/heads/blocked" in refs
    assert (b"refs/heads/allowed" in refs) is not atomic


def published_amend(git):
    old = git.commit({"amend.txt": b"published\n"}, "before amend")
    git.run("push", "origin", "main")
    git.run("commit", "--amend", "-m", "amended published commit")
    git.run("push", f"--force-with-lease=refs/heads/main:{old}", "origin", "main")


REF_WORKFLOWS = [
    *[Workflow("branch-" + mode, {"orphan": ("G07",), "update": ("G16",), "copy": ("G18",), "rename": ("G18", "G27"), "delete": ("G17",), "prune": ("G17", "G28")}[mode], ("switch", "commit", "push") if mode == "orphan" else ("branch", "push", "fetch") if mode == "prune" else ("branch", "push"), partial(branch_workflow, mode=mode), MULTI_GAP if mode == "rename" else DELETE_GAP if mode in {"delete", "prune"} else "") for mode in ("update", "copy", "orphan", "rename", "delete", "prune")],
    *[Workflow("rewrite-" + mode, ("G25",), ("reset", "push", "ls-remote"), partial(rewrite_workflow, mode=mode), "" if mode == "stale-lease" else REWRITE_GAP) for mode in ("force", "lease", "plus-refspec", "stale-lease")],
    *[Workflow("tag-" + mode, ("G21",) if mode in {"overwrite", "delete"} else ("G11", "G20") if mode in {"annotated", "nested"} else ("G20",), ("tag", "push"), partial(tag_workflow, mode=mode), DELETE_GAP if mode == "delete" else "" if mode == "overwrite" else TYPED_GAP) for mode in ("annotated", "nested", "blob", "tree", "overwrite", "delete")],
    *[Workflow("ref-" + mode, ("G22",) if mode == "notes" else ("G23",), ("notes",) if mode == "notes" else ("replace",) if mode == "replace" else ("update-ref",), partial(generic_ref, mode=mode), GENERIC_GAP) for mode in ("notes", "replace", "custom")],
    *[Workflow("push-" + mode, ("G27",) if mode == "atomic" else ("G28",) if mode == "mirror" else ("G26",), ("push",), partial(multi_ref, mode=mode), MULTI_GAP) for mode in ("all", "tags", "atomic", "mirror")],
    *[Workflow("mixed-batch-" + ("atomic" if atomic else "partial"), ("G27",) if atomic else ("G26",), ("push", "ls-remote"), partial(mixed_batch, atomic=atomic), MULTI_GAP if atomic else "mixed non-atomic push fails HTTP 400 instead of reporting per-ref outcomes") for atomic in (False, True)],
    Workflow("published-amend", ("G25", "G36"), ("commit", "push"), published_amend, REWRITE_GAP),
]
