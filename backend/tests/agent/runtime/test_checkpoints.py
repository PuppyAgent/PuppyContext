"""Conversation manifests survive PostgreSQL persistence and reject corruption."""

import copy
from uuid import uuid4

import pytest

from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def saved_workspace(postgres, submitted):
    _, submitted_run = submitted
    run = postgres.rpc("claim", worker="checkpoint-test")
    assert run["id"] == submitted_run["id"]
    checkpoints = Checkpoints()
    value = {
        "version": 2,
        "pi_version": "0.85.1",
        "reason": "prepared",
        "entries": [{"message": {"role": "user", "content": "未提交的写作内容"}}],
        "leaf_id": None,
        "recovery": {"provider": "docker", "id": str(uuid4()), "project_id": run["project_id"]},
    }
    manifest = await checkpoints.save(run, value)
    updated = postgres.rpc(
        "write",
        run=run["id"],
        execution=run["execution_id"],
        fence=run["fence"],
        kind="checkpoint",
        payload={"reason": "prepared"},
        patch={"checkpoint": manifest},
    )
    return checkpoints, updated, value, updated["checkpoint"]


async def test_checkpoint_retry_is_content_addressed_and_does_not_mutate_prior_save(
    saved_workspace,
):
    checkpoints, run, value, manifest = saved_workspace
    assert await checkpoints.save(run, value) == manifest
    updated = copy.deepcopy(value)
    updated["entries"].append({"message": {"role": "assistant", "content": "next"}})
    second = await checkpoints.save(run, updated)
    assert second["sha256"] != manifest["sha256"]
    assert await checkpoints.load(run, manifest) == value
    assert await checkpoints.load(run, second) == updated
    assert second["state"]["recovery"] == manifest["state"]["recovery"]


@pytest.mark.parametrize("damage", ["state", "checksum"])
async def test_corrupt_recovery_manifest_is_rejected(saved_workspace, damage):
    checkpoints, run, _, manifest = saved_workspace
    bad = copy.deepcopy(manifest)
    if damage == "state":
        bad["state"]["entries"].clear()
    else:
        bad["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        await checkpoints.load(run, bad)


@pytest.mark.parametrize("binding", ["project_id", "id"])
async def test_checkpoint_cannot_be_loaded_by_another_project_or_run(saved_workspace, binding):
    checkpoints, run, _, manifest = saved_workspace
    with pytest.raises(ValueError, match="binding"):
        await checkpoints.load({**run, binding: str(uuid4())}, manifest)
