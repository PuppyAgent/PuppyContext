"""Legacy projected Scope restrictions are NOT native full-repository gaps.

ISSUE-062 M16 preserves this separate contract. These negative cases do not
satisfy the corresponding full Project profile's positive target tests.
"""

import pytest

from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.transport.test_stock_git_http import AUTH
from tests.repository_hosting.transport.test_stock_git_http import hosted as hosted

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("hosted", ["docs"], indirect=True, ids=["legacy-scope"])
@pytest.mark.parametrize("operation", ["delete", "force", "merge", "annotated-tag", "notes", "atomic", "blob-tag"])
def test_legacy_rejection_leaves_all_refs_unchanged(hosted, operation):
    git, _, _, _, initial = hosted
    if operation == "delete":
        git.run("push", "origin", "main:refs/heads/delete-me")
        args = [":refs/heads/delete-me"]
    elif operation == "force":
        git.commit({"later": b"later"})
        git.run("push", "origin", "main")
        git.run("reset", "--hard", initial)
        args = ["--force-with-lease", "main"]
    elif operation == "merge":
        git.run("checkout", "-b", "topic")
        git.commit({"topic": b"topic"})
        git.run("checkout", "main")
        git.commit({"main": b"main"})
        git.run("merge", "--no-ff", "topic", "-m", "merge")
        args = ["main"]
    elif operation == "annotated-tag":
        git.run("tag", "-a", "annotated", "-m", "tag body")
        args = ["annotated"]
    elif operation == "notes":
        git.run("notes", "add", "-m", "review metadata")
        args = ["refs/notes/commits"]
    elif operation == "atomic":
        args = ["--atomic", "main:refs/heads/one", "main:refs/heads/two"]
    else:
        git.run("tag", "blob", git.text("rev-parse", "HEAD:original.txt"))
        args = ["blob"]
    before = git.run("ls-remote", "origin").stdout
    rejected = git.run("push", "origin", *args, check=False)
    assert rejected.returncode != 0
    assert git.run("ls-remote", "origin").stdout == before


@pytest.mark.parametrize("hosted", ["docs"], indirect=True, ids=["legacy-scope"])
def test_legacy_shallow_fetch_can_be_deepened(hosted, tmp_path):
    git, remote, _, _, _ = hosted
    git.commit({"next": b"next"})
    git.run("push", "origin", "main")
    destination = tmp_path / "shallow"
    git.run("-c", AUTH, "clone", "--depth=1", remote, destination)
    restored = Git(destination)
    assert restored.text("rev-parse", "--is-shallow-repository") == "true"
    restored.run("-c", AUTH, "fetch", "--unshallow", "origin")
    assert restored.text("rev-parse", "--is-shallow-repository") == "false"
    assert restored.text("rev-parse", "HEAD") == git.text("rev-parse", "HEAD")
