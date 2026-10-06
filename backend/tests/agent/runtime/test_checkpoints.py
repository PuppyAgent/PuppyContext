"""Recovery bytes must be intact and bound to the owning Project and run."""

import base64
import copy
from uuid import uuid4

import pytest

from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def saved_workspace(services, submitted):
    storage = services[1]
    checkpoints = Checkpoints(storage)
    run = submitted[1]
    value = {
        "version": 1,
        "pi_version": "0.85.1",
        "reason": "prepared",
        "entries": [],
        "leaf_id": None,
        "files": {"章节.md": base64.b64encode("未提交的写作内容".encode()).decode()},
        "modes": {"章节.md": "100644"},
        "git": {
            "object_format": "sha256",
            "head": base64.b64encode(b"refs/heads/knowledge").decode(),
            "bundle": None,
            "index": None,
            "tip": None,
        },
    }
    manifest = await checkpoints.save(run, value)
    return storage, checkpoints, run, value, manifest


async def test_checkpoint_retry_is_content_addressed_and_does_not_mutate_prior_save(
    saved_workspace,
):
    _, checkpoints, run, value, manifest = saved_workspace
    assert await checkpoints.save(run, value) == manifest
    updated = copy.deepcopy(value)
    updated["files"]["章节.md"] = base64.b64encode(b"next version").decode()
    next_manifest = await checkpoints.save(run, updated)
    assert next_manifest["key"] != manifest["key"]
    assert await checkpoints.load(run, manifest) == value
    assert await checkpoints.load(run, next_manifest) == updated


@pytest.mark.parametrize("damage", ["same_size_corruption", "truncation"])
async def test_corrupt_recovery_object_is_rejected_before_restoring_workspace(
    saved_workspace, damage
):
    storage, checkpoints, run, _, manifest = saved_workspace
    data = bytearray(await storage.download_file(manifest["key"]))
    if damage == "truncation":
        data = data[:-1]
    else:
        data[len(data) // 2] ^= 0xFF
    await storage.upload_file(manifest["key"], bytes(data), content_type="application/gzip")
    with pytest.raises(ValueError, match="checksum mismatch"):
        await checkpoints.load(run, manifest)


@pytest.mark.parametrize("binding", ["project_id", "id"])
async def test_checkpoint_cannot_be_loaded_by_another_project_or_run(saved_workspace, binding):
    _, checkpoints, run, _, manifest = saved_workspace
    with pytest.raises(ValueError, match="Project binding mismatch"):
        await checkpoints.load({**run, binding: str(uuid4())}, manifest)
