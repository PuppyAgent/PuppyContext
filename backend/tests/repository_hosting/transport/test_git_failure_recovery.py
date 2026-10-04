"""Stock clients at the publication boundary; disk storage and memory DB doubles.

These failures are injected in production paths, not an actual PG/S3 outage or
TCP fault. Real-service failure/cutover acceptance remains a separate gate.
"""

import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from tests.repository_hosting.harness.http import clone_client

pytestmark = pytest.mark.hosting_component


def cold_clone(hosted, path):
    source, remote, state, _, _ = hosted
    assert state.git_cache_root.is_relative_to(path.parent)
    shutil.rmtree(state.git_cache_root)
    return clone_client(remote, path, source)


@pytest.mark.parametrize("boundary", ["object-write", "publication"])
def test_failed_git_push_keeps_confirmed_data_and_can_be_retried(
    hosted, tmp_path, monkeypatch, boundary,
):
    git, _, state, _, before = hosted
    wanted = git.commit({"proposal.txt": b"must not disappear\n"}, "proposed write")
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise OSError("injected publication boundary failure")

    with monkeypatch.context() as fault:
        if boundary == "object-write":
            fault.setattr(state.repo.store._backend, "put", fail)
        else:
            fault.setattr(state.repo.history, "publish_project_update", fail)
        failed = git.run("push", "origin", "main", check=False)
    assert calls and failed.returncode != 0, failed.stderr.decode(errors="replace")
    assert git.text("rev-parse", "HEAD") == wanted
    assert (git.path / "proposal.txt").read_bytes() == b"must not disappear\n"
    reader = cold_clone(hosted, tmp_path / "after-failure")
    assert reader.text("rev-parse", "HEAD") == before
    assert (reader.path / "original.txt").read_bytes() == b"original\n"
    assert not (reader.path / "proposal.txt").exists()
    git.run("push", "origin", "main")
    restored = cold_clone(hosted, tmp_path / "after-retry")
    assert restored.text("rev-parse", "HEAD") == wanted
    assert (restored.path / "proposal.txt").read_bytes() == b"must not disappear\n"
    restored.run("fsck", "--full", "--strict")


def test_lost_git_ack_is_reconciled_by_fetch_and_idempotent_push(hosted, tmp_path, monkeypatch):
    git, _, state, _, _ = hosted
    wanted = git.commit({"ack.txt": b"durably accepted at the test boundary\n"}, "lost ack")
    publish = state.repo.history.publish_project_update
    accepted = []

    def lose_ack(**kwargs):
        result = publish(**kwargs)
        accepted.append(result[0])
        raise OSError("injected lost acknowledgement after publication")

    with monkeypatch.context() as fault:
        fault.setattr(state.repo.history, "publish_project_update", lose_ack)
        response = git.run("push", "origin", "main", check=False)
    assert accepted == [True] and response.returncode != 0
    restored = cold_clone(hosted, tmp_path / "after-lost-ack")
    assert restored.text("rev-parse", "HEAD") == wanted
    assert (restored.path / "ack.txt").read_bytes() == b"durably accepted at the test boundary\n"
    # Retry the same stock-Git operation after discovering the confirmed OID.
    # There must be no second publication, not merely an equal final tree.
    publications = []

    def count_publications(**kwargs):
        publications.append(kwargs)
        return publish(**kwargs)

    monkeypatch.setattr(state.repo.history, "publish_project_update", count_publications)
    git.run("fetch", "origin")
    assert git.text("rev-parse", "origin/main") == wanted
    git.run("push", "origin", "main")
    assert publications == []
    final = cold_clone(hosted, tmp_path / "after-ack-retry")
    assert final.text("rev-parse", "HEAD") == wanted


def test_two_git_writers_from_one_head_have_one_winner_and_keep_loser_work(
    hosted, tmp_path, monkeypatch,
):
    source, remote, state, _, _ = hosted
    clients = [clone_client(remote, tmp_path / f"writer-{i}", source) for i in range(2)]
    tips = [client.commit({"original.txt": f"writer {i}\n".encode()}, f"writer {i}") for i, client in enumerate(clients)]
    publish = state.repo.history.publish_project_update
    barrier, lock = Barrier(2), Lock()
    calls = []

    def simultaneous(**kwargs):
        with lock:
            calls.append(kwargs)
        return publish(**kwargs)

    def start_together(client):
        # Synchronize clients, not a section protected by the server's cache
        # lock. A barrier inside that section invents a deadlock. The service
        # may reject the stale writer before it needs a database publication.
        barrier.wait(timeout=15)
        return client.run("push", "origin", "main", check=False)

    monkeypatch.setattr(state.repo.history, "publish_project_update", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(start_together, clients))
    assert calls
    assert sum(result.returncode == 0 for result in results) == 1, [
        result.stderr.decode(errors="replace") for result in results
    ]
    winner = next(i for i, result in enumerate(results) if result.returncode == 0)
    restored = cold_clone(hosted, tmp_path / "after-race")
    assert restored.text("rev-parse", "HEAD") == tips[winner]
    assert (restored.path / "original.txt").read_bytes() == f"writer {winner}\n".encode()
    loser = 1 - winner
    assert clients[loser].text("rev-parse", "HEAD") == tips[loser]
    assert (clients[loser].path / "original.txt").read_bytes() == f"writer {loser}\n".encode()
