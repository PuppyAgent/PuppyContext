"""Workspace recovery and conversation cost contracts, independent of a provider."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints
from src.platform.access.adapters.agent.runtime.recovery import Recovery

pytestmark = pytest.mark.asyncio


async def test_read_tools_reuse_recovery_without_provider_or_file_io():
    worker = SimpleNamespace(snapshot=AsyncMock(side_effect=AssertionError("read took a snapshot")))
    state = {"recovery": {"provider": "docker", "id": "previous"}}
    for tool in ("ls", "read", "find", "grep"):
        result = await Recovery().after_tool(worker, state, {"name": tool})
        assert result["recovery"] == state["recovery"]
    worker.snapshot.assert_not_awaited()


@pytest.mark.parametrize("name", ["write", "edit", "bash", "new_unknown_tool"])
async def test_mutations_even_failed_ones_save_provider_recovery(name):
    worker = SimpleNamespace(snapshot=AsyncMock(return_value={"id": "after"}))
    result = await Recovery().after_tool(
        worker, {"recovery": {"id": "before"}}, {"name": name, "result": {"isError": True}}
    )
    assert result["recovery"] == {"id": "after"}
    worker.snapshot.assert_awaited_once()


async def test_conversation_checkpoint_is_bound_immutable_and_contains_no_workspace_bytes():
    checkpoints = Checkpoints()
    run = {"id": "run", "project_id": "project"}
    value = {"entries": [{"text": "中文"}], "recovery": {"id": "provider-snapshot"}}
    saved = await checkpoints.save(run, value)
    value["entries"].clear()
    assert (await checkpoints.load(run, saved))["entries"] == [{"text": "中文"}]
    with pytest.raises(ValueError, match="binding"):
        await checkpoints.load({**run, "project_id": "other"}, saved)
    damaged = copy.deepcopy(saved)
    damaged["state"]["entries"].clear()
    with pytest.raises(ValueError, match="checksum"):
        await checkpoints.load(run, damaged)
    for field in ("files", "base_files", "modes", "git"):
        with pytest.raises(ValueError, match="Workspace bytes"):
            await checkpoints.save(run, {field: {}})


async def test_unchanged_finalization_does_not_take_another_snapshot():
    worker = SimpleNamespace(
        control=AsyncMock(return_value={"changed": False}),
        snapshot=AsyncMock(side_effect=AssertionError("unchanged finalization copied files")),
    )
    state = {"workspace": {}, "recovery": {"id": "original"}}
    result = await Recovery().finalize(worker, state, {"id": "run"})
    assert result["recovery"] == state["recovery"]
    worker.control.assert_awaited_once_with("finalize", {"message": "Agent run run"})


async def test_pending_publication_is_not_reclassified_or_replayed():
    from unittest.mock import Mock

    from src.platform.access.adapters.agent.runtime.publication import Publication

    transport = SimpleNamespace(result=Mock(return_value={"status": "pending"}), describe=Mock())
    worker = SimpleNamespace(control=AsyncMock())
    with pytest.raises(RuntimeError, match="still pending"):
        await Publication(transport=transport).publish(
            {"id": "run", "project_id": "project", "policy": {"readonly": False}},
            {"workspace": {"changed": True}},
            object(),
            worker,
        )
    transport.describe.assert_not_called()
    worker.control.assert_not_awaited()


async def test_provider_reconnect_deduplicates_replayed_control_and_tool_frames():
    import json

    from src.platform.scope_sandbox.execution.pi_worker import PiWorker
    from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore

    worker = PiWorker(
        "execution", "project", provider="docker", store=InMemoryExecutionSessionStore()
    )
    first = {"sequence": 1, "type": "tool_end", "id": "once"}
    second = {"sequence": 2, "type": "text", "text": "next"}
    await worker._output(json.dumps(first) + "\n")
    await worker._output(json.dumps(first) + "\n" + json.dumps(second) + "\n")
    assert await worker.receive() == first
    assert await worker.receive() == second
    assert worker.queue.empty()
    with pytest.raises(RuntimeError, match="protocol"):
        await worker._output('{"type":"ready"}\n')


async def test_control_disconnect_fails_pending_command_without_timeout():
    import asyncio

    from src.platform.scope_sandbox.execution.pi_worker import PiWorker
    from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore
    from src.platform.scope_sandbox.execution.worker_port import WorkerDisconnected

    worker = PiWorker(
        "execution", "project", provider="docker", store=InMemoryExecutionSessionStore()
    )
    worker.send = AsyncMock()
    command = asyncio.create_task(worker.control("snapshot"))
    await asyncio.sleep(0)
    await worker._disconnected()
    with pytest.raises(WorkerDisconnected):
        await asyncio.wait_for(command, 0.1)
    assert not worker.controls


async def test_agent_runtime_cannot_own_object_storage_or_git_codecs():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src/platform/access/adapters/agent/runtime"
    forbidden = (
        "src.infra.s3",
        "src.version_engine.storage",
        "src.version_engine.adapters.git.object_pack",
        "src.version_engine.write_engine.git_object",
    )
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            )
            assert not any(name.startswith(forbidden) for name in names), path
    assert not (root / "git_workspace.py").exists()
