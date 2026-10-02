"""Product retry/review regressions; disk objects and explicit ledger doubles."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.storage.backends.s3 import CachedStorageBackend, ObjectWriteBatch
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.conflict_queue import (
    pending_conflict_id,
    record_pending_conflict,
)
from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.write_engine.tree_objects import flatten_tree_to_bytes
from tests.repository_hosting.harness.catalog import MemoryLedger

pytestmark = pytest.mark.hosting_component


def operation_identity(**overrides):
    args = dict(
        project_id="p", scope_path="docs", current_head_commit_id="head",
        client_commit_id="", paths=["a", "b"], proposed_tree_id="proposal",
        base_commit_id="base", actor="user:a", source_channel="papi",
        policy="manual_review",
    )
    args.update(overrides)
    return pending_conflict_id(**args)


@pytest.mark.parametrize("field,value", [
    ("project_id", "other"), ("scope_path", "other"),
    ("current_head_commit_id", "other"), ("proposed_tree_id", "other"),
    ("base_commit_id", "other"), ("actor", "user:b"),
    ("source_channel", "agent"), ("policy", "agent_review"), ("paths", ["c"]),
])
def test_distinct_operation_proposals_do_not_share_a_pending_identity(field, value):
    assert operation_identity(**{field: value}) != operation_identity()


def test_same_operation_proposal_has_stable_identity():
    assert operation_identity() == operation_identity(paths=["b", "a", "a"])


def test_empty_commit_requires_proposal_identity():
    with pytest.raises(ValueError, match="proposed tree"):
        operation_identity(proposed_tree_id="")


def test_git_conflict_identity_preserves_existing_retry_contract():
    old = dict(
        project_id="p", scope_path="docs", current_head_commit_id="head",
        client_commit_id="client", paths=["a", "b"],
    )
    expected = hashlib.sha1(json.dumps(
        old, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert pending_conflict_id(**old) == expected


@pytest.mark.asyncio
async def test_failed_pending_persistence_is_not_acknowledged():
    ledger = SimpleNamespace(
        insert_version_transaction=Mock(return_value=1),
        record_pending_conflict=Mock(side_effect=OSError("ledger unavailable")),
    )
    with pytest.raises(OSError, match="ledger unavailable"):
        await record_pending_conflict(
            ledger=ledger, repo=SimpleNamespace(record_audit=Mock()),
            project_id="p", scope_path="", current_head_commit_id="head",
            current_scope_hash="current", client_commit_id="", base_commit_id="base",
            proposed_tree_id="proposal", source_channel="papi", actor="user:a",
            message="proposal", audit_detail={}, base_files={}, current_files={},
            incoming_files={}, manual_conflicts=[], policy_reason="test",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "docs"])
async def test_pending_retry_tree_survives_discarding_all_process_caches(
    component_repo, monkeypatch, scope,
):
    state = component_repo
    physical = state.repo.store._backend
    state.repo.store = ObjectStore(None, backend=CachedStorageBackend(physical))
    ledger = MemoryLedger()
    state.adapter._engine = VersionWriteEngine(state.manager, ledger=ledger)
    if scope:
        state.repo.add_scope("scope-docs", scope)
    await state.adapter.write_file(
        "test-proj", "f.txt", b"base\n", who="user:seed", scope=scope,
    )
    snapshot = ProjectWriteState(
        "test-proj", "Recovery", can_write=True,
        root_hash=state.repo.history.get_root_hash(),
        head_commit_id=state.repo.history.get_head_commit_id(),
    )

    async def competing_write():
        await state.adapter.bulk_write(
            "test-proj", {"f.txt": b"current\n", "other.txt": b"keep\n"},
            who="user:a", scope=scope,
        )

    if scope:
        # Scoped operations obtain their base inside the writer, so interleave
        # another writer precisely at the first publication attempt.
        from src.version_engine.write_engine import operation_writer

        publish = operation_writer._publish_project_update
        interleaved = False

        async def publish_after_competing_write(**kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                await competing_write()
            return await publish(**kwargs)

        monkeypatch.setattr(
            operation_writer, "_publish_project_update", publish_after_competing_write,
        )
    else:
        await competing_write()
    result = await state.adapter.write_file(
        "test-proj", "f.txt", b"proposal\n", who="user:b", policy="manual_review",
        project_write_state=snapshot, scope=scope,
    )
    assert result.status == "pending"
    pending = next(iter(ledger.pending.values()))
    # Bypass BOTH write-batch and read-through caches, as another process would.
    reopened = ObjectStore(None, backend=physical)
    assert flatten_tree_to_bytes(reopened, pending["proposed_tree_id"]) == {
        "f.txt": b"proposal\n", "other.txt": b"keep\n",
    }
    prefix = scope + "/" if scope else ""
    assert flatten_tree_to_bytes(reopened, state.repo.history.get_root_hash()) == {
        prefix + "f.txt": b"current\n", prefix + "other.txt": b"keep\n",
    }


@pytest.mark.asyncio
async def test_failed_retry_flush_does_not_acknowledge_or_queue(component_repo, monkeypatch):
    state = component_repo
    state.repo.store = ObjectStore(None, backend=CachedStorageBackend(state.repo.store._backend))
    ledger = MemoryLedger()
    state.adapter._engine = VersionWriteEngine(state.manager, ledger=ledger)
    await state.adapter.write_file("test-proj", "f.txt", b"base\n", who="user:seed")
    snapshot = ProjectWriteState(
        "test-proj", "Recovery", can_write=True,
        root_hash=state.repo.history.get_root_hash(),
        head_commit_id=state.repo.history.get_head_commit_id(),
    )
    await state.adapter.bulk_write(
        "test-proj", {"f.txt": b"current\n", "other.txt": b"keep\n"}, who="user:a",
    )
    before = state.repo.history.get_root_hash()
    flush = ObjectWriteBatch.flush
    calls = 0

    def fail_retry_flush(batch):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("retry storage unavailable")
        return flush(batch)

    monkeypatch.setattr(ObjectWriteBatch, "flush", fail_retry_flush)
    with pytest.raises(OSError, match="retry storage unavailable"):
        await state.adapter.write_file(
            "test-proj", "f.txt", b"proposal\n", who="user:b",
            policy="manual_review", project_write_state=snapshot,
        )
    assert calls == 2
    assert ledger.pending == {} and ledger.transactions == []
    assert state.repo.history.get_root_hash() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("files,source", [
    ({"source.txt": b"original\n"}, "source.txt"),
    ({"source/a.txt": b"a\n", "source/nested/b.txt": b"b\n"}, "source"),
])
async def test_rename_retries_from_admitted_snapshot_not_a_new_live_lookup(
    component_repo, files, source,
):
    state = component_repo
    await state.adapter.bulk_write("test-proj", files, who="user:seed")
    snapshot = ProjectWriteState(
        "test-proj", "Rename", can_write=True,
        root_hash=state.repo.history.get_root_hash(),
        head_commit_id=state.repo.history.get_head_commit_id(),
    )
    await state.adapter.move("test-proj", source, "first", who="user:a")
    result = await state.adapter.move(
        "test-proj", source, "second", who="user:b", project_write_state=snapshot,
    )
    assert result.status == "ok" and result.commit_id
    expected = {
        destination + path[len(source):]: body
        for path, body in files.items() for destination in ("first", "second")
    }
    assert flatten_tree_to_bytes(state.repo.store, state.repo.history.get_root_hash()) == expected


@pytest.mark.asyncio
async def test_move_without_source_in_first_snapshot_is_still_rejected(component_repo):
    state = component_repo
    before = state.repo.history.get_root_hash()
    with pytest.raises(FileNotFoundError):
        await state.adapter.move("test-proj", "missing", "destination", who="user:a")
    assert state.repo.history.get_root_hash() == before
