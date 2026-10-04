import pytest

from src.version_engine.write_engine.tree_objects import flatten_tree_to_bytes

pytestmark = pytest.mark.hosting_component


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["object_write", "database_publish"])
async def test_failure_before_publish_keeps_old_head_and_files(
    component_repo, monkeypatch, boundary
):
    state = component_repo
    await state.adapter.write_file(
        "test-proj", "f", b"saved", who="user:test", defer_projection=True
    )
    before = state.repo.history.get_root_hash(), state.repo.history.get_head_commit_id()
    hit = []

    def fail(*a, **k):
        hit.append(True)
        raise RuntimeError("injected " + boundary)

    if boundary == "object_write":
        monkeypatch.setattr(state.repo.store._backend, "put", fail)
    else:
        monkeypatch.setattr(state.repo.history, "publish_project_update", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await state.adapter.write_file(
            "test-proj", "f", b"new", who="user:test", defer_projection=True
        )
    assert hit
    assert (state.repo.history.get_root_hash(), state.repo.history.get_head_commit_id()) == before
    assert flatten_tree_to_bytes(state.repo.store, before[0])["f"] == b"saved"


@pytest.mark.asyncio
async def test_lost_ack_keeps_committed_object_and_published_head(component_repo, monkeypatch):
    state = component_repo
    original = state.repo.history.publish_project_update
    hit = []

    def lose_ack(**kwargs):
        result = original(**kwargs)
        hit.append(result)
        raise RuntimeError("injected lost ack after commit")

    monkeypatch.setattr(state.repo.history, "publish_project_update", lose_ack)
    with pytest.raises(RuntimeError, match="lost ack"):
        await state.adapter.write_file(
            "test-proj", "f", b"saved", who="user:test", defer_projection=True
        )
    assert hit and hit[0][0]
    oid = state.repo.history.get_head_commit_id()
    assert state.repo.store.get_object(oid)[0] == "commit"
    assert (
        flatten_tree_to_bytes(state.repo.store, state.repo.history.get_root_hash())["f"] == b"saved"
    )
