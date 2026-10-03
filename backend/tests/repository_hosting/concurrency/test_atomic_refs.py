from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_native


def test_exactly_one_native_ref_cas_wins(git_repo):
    old = git_repo.commit({"a": b"base"})
    tree = git_repo.text("rev-parse", "HEAD^{tree}")
    candidates = [
        git_repo.run("commit-tree", tree, "-p", old, input=f"writer {i}".encode())
        .stdout.strip()
        .decode()
        for i in range(8)
    ]
    gate = Barrier(len(candidates))

    def write(oid):
        gate.wait(timeout=10)
        return oid, git_repo.run("update-ref", "refs/heads/main", oid, old, check=False).returncode

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(write, candidates))
    winners = [oid for oid, rc in outcomes if rc == 0]
    assert len(winners) == 1
    assert git_repo.text("rev-parse", "main") == winners[0]


def test_failed_batch_does_not_advance_any_ref(git_repo):
    old = git_repo.commit({"a": b"base"})
    new = git_repo.commit({"a": b"new"}, "new")
    git_repo.run("branch", "other", old)
    before = git_repo.refs()
    body = f"start\nupdate refs/heads/main {old} {new}\nupdate refs/heads/other {new} {new}\nprepare\ncommit\n".encode()
    assert git_repo.run("update-ref", "--stdin", input=body, check=False).returncode != 0
    assert git_repo.refs() == before


def test_ref_prefix_create_race_has_one_winner(git_repo, tmp_path):
    oid = git_repo.commit({"a": b"base"})
    # Hosting ref transactions use a stock bare repository as their oracle,
    # not a worktree with implicit filesystem reflogs. Git 2.50.1's worktree
    # reflog D/F race can reject both writers (recorded separately); it is not
    # a guarantee of our PostgreSQL reflog implementation. Retain the exact
    # one-winner/ref-state assertions against the declared hosting profile.
    bare = tmp_path / "prefix-oracle.git"
    git_repo.run("clone", "--bare", "--no-hardlinks", git_repo.path, bare)
    git_repo = Git(bare)
    assert git_repo.text("rev-parse", "--is-bare-repository") == "true"
    gate = Barrier(2)

    def create(name):
        gate.wait(timeout=10)
        return git_repo.run("update-ref", name, oid, "0" * 40, check=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["refs/heads/topic", "refs/heads/topic/child"]))
    assert sorted(p.returncode == 0 for p in results) == [False, True], [
        p.stderr.decode(errors="replace") for p in results
    ]
    assert len([name for name in git_repo.refs() if name.startswith("refs/heads/topic")]) == 1
