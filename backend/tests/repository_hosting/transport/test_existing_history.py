"""Existing advertised ancestry is a receiver dependency, not client pack data.

Production HTTP with disk objects and memory control plane; NOT S3 durability.
Run on both Project and unchanged legacy Scope profiles.
"""

import shutil

import pytest

from src.version_engine.adapters.git import object_quarantine
from src.version_engine.adapters.git.view_projection import _is_valid_tree
from src.version_engine.write_engine.git_object_format import TreeEntry, encode_tree
from tests.repository_hosting.harness.conformance import remote_snapshot
from tests.repository_hosting.harness.http import AUTH

pytestmark = pytest.mark.hosting_component


def evict(hosted, tmp_path):
    cache = hosted[2].git_cache_root
    assert cache.is_relative_to(tmp_path)
    shutil.rmtree(cache)


def cold_snapshot(hosted, tmp_path):
    client, remote, _, _, _ = hosted
    evict(hosted, tmp_path)
    return remote_snapshot(client, remote, tmp_path / "cold.git")


def test_revert_uses_existing_ancestry_without_resending_it(hosted, tmp_path):
    client, _, _, _, initial = hosted
    client.commit({"one": b"one\n"}, "one")
    last = client.commit({"two": b"two\n"}, "two")
    client.run("push", "origin", "main")
    client.run("revert", "--no-edit", initial + ".." + last)
    wanted = client.text("rev-parse", "HEAD")
    assert client.text("rev-parse", "HEAD^{tree}") == client.text("rev-parse", initial + "^{tree}")
    evict(hosted, tmp_path)
    client.run("push", "origin", "main")
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {wanted}\n".encode()
    assert snapshot.objects == client.objects()


def test_lightweight_tag_of_existing_ancestor_survives_cold_read(hosted, tmp_path):
    client, _, _, _, _ = hosted
    ancestor = client.commit({"new": b"old version\n"}, "intermediate ancestor")
    head = client.commit({"new": b"new version\n"}, "advance")
    client.run("push", "origin", "main")
    client.run("tag", "ancestor", ancestor)
    evict(hosted, tmp_path)
    client.run("push", "origin", "refs/tags/ancestor")
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {head}\nrefs/tags/ancestor {ancestor}\n".encode()
    assert snapshot.objects == client.objects()


def test_external_gitlink_is_healthy_and_exact_on_cold_read(hosted, tmp_path):
    client, _, state, _, _ = hosted
    external = "12" * 20
    client.run("update-index", "--add", "--cacheinfo", f"160000,{external},module")
    client.run("commit", "-m", "external submodule")
    wanted = client.text("rev-parse", "HEAD")
    client.run("push", "origin", "main")
    assert not state.repo.store.exists(external)
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {wanted}\n".encode()
    assert snapshot.objects == client.objects()
    # An ordinary missing file remains corruption; only mode 160000 is external.
    missing = state.repo.store.put_tree(encode_tree([TreeEntry("missing", b"100644", external)]))
    assert not _is_valid_tree(state.repo, missing, set())


def test_receiver_can_reuse_blob_missing_from_current_tree(hosted, tmp_path):
    client, _, _, _, initial = hosted
    old_blob = client.text("rev-parse", initial + ":original.txt")
    client.commit({"original.txt": b"replacement\n"}, "replace original")
    client.run("push", "origin", "main")
    client.run("update-index", "--add", "--cacheinfo", f"100644,{old_blob},reused.txt")
    client.run("commit", "-m", "reuse old blob")
    wanted = client.text("rev-parse", "HEAD")
    evict(hosted, tmp_path)
    client.run("push", "origin", "main")
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {wanted}\n".encode()
    assert snapshot.objects == client.objects()
    assert snapshot.objects[old_blob] == ("blob", b"original\n")


def test_stale_client_cannot_overwrite_history_after_receive_repair(hosted, tmp_path):
    client, remote, _, _, initial = hosted
    accepted = client.commit({"accepted": b"keep\n"}, "accepted")
    client.run("push", "origin", "main")
    client.run("reset", "--hard", initial)
    rejected = client.commit({"rejected": b"local\n"}, "divergent")
    outcome = client.run("push", "--force", "origin", "main", check=False)
    assert outcome.returncode != 0
    assert b"non-fast-forward" in outcome.stderr
    assert client.text("rev-parse", "HEAD") == rejected
    assert (client.path / "rejected").read_bytes() == b"local\n"
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {accepted}\n".encode()
    assert rejected not in snapshot.objects
    assert client.run("-c", AUTH, "ls-remote", remote, "refs/heads/main").stdout == f"{accepted}\trefs/heads/main\n".encode()


@pytest.mark.parametrize("ref,existing", [
    ("refs/heads/main", False), ("refs/heads/topic", False),
    ("refs/heads/topic", True), ("refs/tags/existing", True),
])
def test_stock_receive_rejection_cannot_be_overridden_by_object_presence(
    hosted, tmp_path, monkeypatch, ref, existing,
):
    client, remote, state, _, initial = hosted
    wanted = initial if existing else client.commit({"rejected": b"unpublished\n"}, "receiver rejects")
    run = object_quarantine._run_official_receive_pack
    observed = []

    def reject(bare, request_path):
        hook = bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'injected receiver rejection' >&2\nexit 1\n")
        hook.chmod(0o700)
        result = run(bare, request_path)
        observed.append(result)
        return result

    with monkeypatch.context() as fault:
        fault.setattr(object_quarantine, "_run_official_receive_pack", reject)
        outcome = client.run("push", "origin", "HEAD:" + ref, check=False)
    assert observed and b"pre-receive hook declined" in observed[0]
    assert outcome.returncode != 0
    assert client.text("rev-parse", "HEAD") == wanted
    if not existing:
        assert not state.repo.store.exists(wanted), "rejected objects must stay in quarantine"
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {initial}\n".encode()
    if not existing:
        assert wanted not in snapshot.objects
    client.run("push", "origin", "HEAD:" + ref)
    assert client.run("-c", AUTH, "ls-remote", remote, ref).stdout == f"{wanted}\t{ref}\n".encode()


@pytest.mark.parametrize("failure_call", [1, 2], ids=["advertise", "receive"])
def test_ref_snapshot_outage_cannot_be_treated_as_an_empty_namespace(
    hosted, tmp_path, monkeypatch, failure_call,
):
    from src.version_engine.infrastructure.supabase.version_ref_repository import VersionRefStore

    client, _, _, _, initial = hosted
    wanted = client.commit({"pending": b"retry after recovery\n"}, "database outage")
    list_refs = VersionRefStore.list_refs
    calls = []

    def unavailable(self, *args, **kwargs):
        calls.append(kwargs)
        if len(calls) == failure_call:
            assert kwargs.get("strict") is True
            raise RuntimeError("injected control-plane outage")
        return list_refs(self, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(VersionRefStore, "list_refs", unavailable)
        result = client.run("push", "origin", "HEAD:refs/heads/topic", check=False)
    assert len(calls) >= failure_call and result.returncode != 0
    assert client.text("rev-parse", "HEAD") == wanted
    snapshot = cold_snapshot(hosted, tmp_path)
    assert snapshot.refs == f"refs/heads/main {initial}\n".encode()
    assert wanted not in snapshot.objects
    client.run("push", "origin", "HEAD:refs/heads/topic")
