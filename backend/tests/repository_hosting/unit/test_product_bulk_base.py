"""Bulk preconditions through the existing Product funnel, not native activation."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.entrypoints.http.content_write import bulk_write
from src.version_engine.entrypoints.http.schemas import BulkWriteRequest
from src.version_engine.write_engine.engine import ConcurrentMutationError
from src.version_engine.write_engine.tree_objects import flatten_tree_to_bytes

pytestmark = pytest.mark.hosting_component
PROJECT = "test-proj"


def acknowledged_state(repo):
    return deepcopy((repo.history.get_root_hash(), repo.history._entries, repo.audit.events))


@pytest.mark.asyncio
async def test_http_bulk_preserves_the_callers_stale_base(component_repo):
    state = component_repo
    first = await state.adapter.write_file(PROJECT, "file.txt", b"base", who="user:seed")
    winner = await state.adapter.write_file(PROJECT, "file.txt", b"winner", who="user:other")
    before = acknowledged_state(state.repo)
    state.manager.get_project_write_state.return_value = ProjectWriteState(
        PROJECT, "Bulk test", role="editor", can_write=True,
        root_hash=state.repo.history.get_root_hash(), head_commit_id=winner.commit_id,
    )
    body = BulkWriteRequest.model_validate({
        "files": [{"path": "file.txt", "content": "loser", "node_type": "file"}],
        "base_commit_id": first.commit_id,
    })
    with pytest.raises(HTTPException) as error:
        await bulk_write(PROJECT, body, VersionWriteCommandService(state.adapter),
                         SimpleNamespace(user_id="actor"))
    assert error.value.status_code == 409
    assert acknowledged_state(state.repo) == before
    assert flatten_tree_to_bytes(state.repo.store, before[0]) == {"file.txt": b"winner"}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "docs"])
@pytest.mark.parametrize("by_reference", [False, True])
@pytest.mark.parametrize("base_kind", ["stale", "empty", "current", "omitted", "unborn"])
async def test_bulk_bytes_and_refs_preserve_explicit_base(
    component_repo, scope, by_reference, base_kind,
):
    state = component_repo
    if scope:
        state.repo.add_scope("docs-scope", scope)
    if base_kind == "unborn":
        base = ""
    else:
        first = await state.adapter.write_file(
            PROJECT, "file.txt", b"base", who="user:seed", scope=scope,
        )
        winner = await state.adapter.write_file(
            PROJECT, "file.txt", b"winner", who="user:other", scope=scope,
        )
        base = {"stale": first.commit_id, "empty": "", "current": winner.commit_id,
                "omitted": None}[base_kind]
    before = acknowledged_state(state.repo)
    commands = VersionWriteCommandService(state.adapter)
    kwargs = {"actor": "user:actor", "scope": scope, "base_commit_id": base}
    if by_reference:
        ref = await state.adapter.stage_blob_from_bytes(PROJECT, b"incoming")
        operation = commands.bulk_write_refs(PROJECT, {"file.txt": ref}, **kwargs)
    else:
        operation = commands.bulk_write(PROJECT, {"file.txt": b"incoming"}, **kwargs)
    if base_kind in {"stale", "empty"}:
        with pytest.raises(ConcurrentMutationError):
            await operation
        assert acknowledged_state(state.repo) == before
        expected = b"winner"
    else:
        result = await operation
        assert result.result.commit_id
        assert len(state.repo.history._entries) == len(before[1]) + 1
        expected = b"incoming"
    prefix = scope + "/" if scope else ""
    assert flatten_tree_to_bytes(state.repo.store, state.repo.history.get_root_hash()) == {
        prefix + "file.txt": expected,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "docs"])
@pytest.mark.parametrize("by_reference", [False, True])
async def test_empty_bulk_does_not_discard_a_precondition(component_repo, scope, by_reference):
    state = component_repo
    if scope:
        state.repo.add_scope("docs-scope", scope)
    current = await state.adapter.write_file(PROJECT, "file.txt", b"ack", who="seed", scope=scope)
    before = acknowledged_state(state.repo)
    commands = VersionWriteCommandService(state.adapter)
    method = commands.bulk_write_refs if by_reference else commands.bulk_write
    with pytest.raises(ConcurrentMutationError):
        await method(PROJECT, {}, actor="actor", scope=scope, base_commit_id="")
    assert acknowledged_state(state.repo) == before
    await method(PROJECT, {}, actor="actor", scope=scope, base_commit_id=current.commit_id)
    assert acknowledged_state(state.repo) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "docs"])
@pytest.mark.parametrize("by_reference", [False, True])
async def test_bulk_cas_retry_does_not_replace_the_original_base(
    component_repo, monkeypatch, scope, by_reference,
):
    from src.version_engine.write_engine import operation_writer

    state = component_repo
    if scope:
        state.repo.add_scope("docs-scope", scope)
    first = await state.adapter.write_file(PROJECT, "file.txt", b"base", who="seed", scope=scope)
    commands = VersionWriteCommandService(state.adapter)
    publish = operation_writer._publish_project_update
    winner_state = None
    competing = False

    async def publish_after_winner(**kwargs):
        nonlocal competing, winner_state
        if not competing:
            competing = True
            await state.adapter.write_file(PROJECT, "file.txt", b"winner", who="other", scope=scope)
            winner_state = acknowledged_state(state.repo)
        return await publish(**kwargs)

    monkeypatch.setattr(operation_writer, "_publish_project_update", publish_after_winner)
    kwargs = {"actor": "actor", "scope": scope, "base_commit_id": first.commit_id}
    with pytest.raises(ConcurrentMutationError):
        if by_reference:
            ref = await state.adapter.stage_blob_from_bytes(PROJECT, b"loser")
            await commands.bulk_write_refs(PROJECT, {"file.txt": ref}, **kwargs)
        else:
            await commands.bulk_write(PROJECT, {"file.txt": b"loser"}, **kwargs)
    assert winner_state is not None
    assert acknowledged_state(state.repo) == winner_state
    prefix = scope + "/" if scope else ""
    assert flatten_tree_to_bytes(state.repo.store, winner_state[0]) == {prefix + "file.txt": b"winner"}


@pytest.mark.asyncio
@pytest.mark.parametrize("by_reference", [False, True])
async def test_one_base_cannot_guard_separate_bulk_transactions(component_repo, monkeypatch, by_reference):
    state = component_repo
    before = acknowledged_state(state.repo)
    monkeypatch.setattr(state.adapter, "_group_paths_by_scope", lambda _project, paths:
                        {"left": ["a"], "right": ["b"]} if paths else {})
    files = {"left/a": b"a", "right/b": b"b"}
    commands = VersionWriteCommandService(state.adapter)
    with pytest.raises(ValueError, match="ambiguous for multi-scope"):
        if by_reference:
            refs = {path: await state.adapter.stage_blob_from_bytes(PROJECT, body)
                    for path, body in files.items()}
            await commands.bulk_write_refs(PROJECT, refs, actor="actor", base_commit_id="")
        else:
            await commands.bulk_write(PROJECT, files, actor="actor", base_commit_id="")
    assert acknowledged_state(state.repo) == before
