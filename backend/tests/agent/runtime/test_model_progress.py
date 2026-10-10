"""Lost sandbox replies must not leave a live lease spinning indefinitely."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import settings
from src.platform.access.adapters.agent.runtime.heartbeat import EndRun
from src.platform.access.adapters.agent.runtime.model_progress import ModelProgress
from src.platform.access.adapters.agent.runtime.runner import RunSupervisor
from src.platform.managed_ai.contracts import InferenceRun, ModelChunk
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore
from src.platform.scope_sandbox.execution.worker_port import WorkerDisconnected


def supervisor():
    return RunSupervisor(None, None, None, None, None, worker_factory=None, worker_lifecycle=None)


def test_model_completion_requires_sandbox_acknowledgement():
    now = [0]
    progress = ModelProgress(timeout=30, clock=lambda: now[0])
    progress.started("model")
    now[0] = 300  # A model still reasoning is governed by the inference deadline.
    progress.check()
    progress.delivered("model")
    now[0] += 29
    progress.check()
    now[0] += 1
    with pytest.raises(EndRun) as error:
        progress.check()
    assert error.value.code == "worker_response_timeout"


@pytest.mark.parametrize("ack_before_end", [False, True])
def test_approval_wait_and_next_model_do_not_inherit_completion_deadline(ack_before_end):
    now = [0]
    progress = ModelProgress(timeout=30, clock=lambda: now[0])
    progress.started("first")
    if ack_before_end:
        progress.acknowledged()
        progress.delivered("first")
    else:
        progress.delivered("first")
        progress.acknowledged()
    now[0] = 600
    progress.check()  # Waiting for a human approval is valid.
    progress.started("next")
    progress.delivered("first")  # Late completion from the previous task.
    now[0] = 1200
    progress.check()


@pytest.mark.asyncio
async def test_background_model_transport_error_reaches_supervisor():
    value = supervisor()

    async def broken():
        raise WorkerDisconnected("lost end frame")

    task = asyncio.create_task(broken())
    await asyncio.sleep(0)
    value.models["request"] = task
    with pytest.raises(WorkerDisconnected):
        await asyncio.wait_for(value.watch_models(), 0.1)


@pytest.mark.asyncio
async def test_stalled_model_response_is_detected_without_database_polling():
    value = supervisor()
    value.model_progress = ModelProgress(timeout=0)
    value.model_progress.started("request")
    value.model_progress.delivered("request")
    with pytest.raises(EndRun, match="worker_response_timeout"):
        await asyncio.wait_for(value.watch_models(), 0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_model_end_delivery_is_not_silenced_or_skipped(monkeypatch, cleanup_fails):
    monkeypatch.setattr(settings, "MANAGED_AI_ENABLED", True)
    value = supervisor()
    value.run = {
        "id": "run",
        "user_id": "user",
        "snapshot": {},
        "policy": {"model": "fixture", "max_tokens": 4096},
    }
    value.conversation_manifest = AsyncMock(return_value=({}, {}))
    value.command = AsyncMock()
    value.repo = SimpleNamespace(begin_model=object())
    value.worker = SimpleNamespace(send=AsyncMock())
    close = AsyncMock(side_effect=RuntimeError("cleanup failed") if cleanup_fails else None)

    async def events():
        yield ModelChunk({"choices": [{"delta": {"content": "partial"}}]})

    value.inference = SimpleNamespace(
        completion=AsyncMock(return_value=InferenceRun("reservation", events(), close))
    )
    value.model_progress.started("request")
    frame = {
        "id": "request",
        "request": {
            "model": "fixture",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
            "stream": True,
        },
        "checkpoint": {},
    }
    if cleanup_fails:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await value._model(frame)
    else:
        await value._model(frame)
    assert value.worker.send.call_args.args[0]["type"] == "model_end"
    assert value.model_progress.completed_at is not None


@pytest.mark.asyncio
async def test_sandbox_send_deadline_includes_lock_contention(monkeypatch):
    import src.platform.scope_sandbox.execution.pi_worker as module

    monkeypatch.setattr(module, "SEND_TIMEOUT", 0.01)
    value = PiWorker("execution", "project", provider="e2b", store=InMemoryExecutionSessionStore())
    await value.send_lock.acquire()
    with pytest.raises(WorkerDisconnected, match="timed out"):
        await value.send({"type": "model_end", "id": "request"})
    value.send_lock.release()


@pytest.mark.asyncio
async def test_sandbox_send_has_bounded_provider_timeout(monkeypatch):
    import src.platform.scope_sandbox.execution.pi_worker as module

    monkeypatch.setattr(module, "SEND_TIMEOUT", 0.01)
    value = PiWorker("execution", "project", provider="e2b", store=InMemoryExecutionSessionStore())
    value.handle = SimpleNamespace(pid=1)

    async def stalled(*args, **kwargs):
        await asyncio.Future()

    send = AsyncMock(side_effect=stalled)
    value.sandbox = SimpleNamespace(commands=SimpleNamespace(send_stdin=send))
    with pytest.raises(WorkerDisconnected, match="timed out"):
        await value.send({"type": "model_end", "id": "request"})
    assert send.call_args.kwargs["request_timeout"] == 0.01


@pytest.mark.asyncio
async def test_snapshot_closes_old_sdk_subscription_before_reconnect():
    value = PiWorker("execution", "project", provider="e2b", store=InMemoryExecutionSessionStore())
    calls = []

    async def disconnect():
        calls.append("disconnect")

    async def snapshot():
        calls.append("snapshot")
        return SimpleNamespace(snapshot_id="snapshot")

    async def wait():
        await asyncio.Future()

    async def connect(*args, **kwargs):
        calls.append("connect")
        return SimpleNamespace(pid=1, wait=wait, disconnect=AsyncMock())

    value.handle = SimpleNamespace(pid=1, disconnect=disconnect)
    value.reader = asyncio.create_task(wait())
    value.resource["template"] = "pinned"
    value.sandbox = SimpleNamespace(
        create_snapshot=snapshot, commands=SimpleNamespace(connect=connect)
    )
    value.control = AsyncMock(return_value={})
    try:
        await value.snapshot()
        assert calls == ["disconnect", "snapshot", "connect"]
        assert [call.args[0] for call in value.control.call_args_list] == ["freeze", "thaw"]
        assert not value.reconnecting
    finally:
        value.reconnecting = True
        await value._disconnect_output()


@pytest.mark.asyncio
async def test_output_replay_deduplicates_but_missing_frame_is_not_silent():
    value = PiWorker("execution", "project", provider="e2b", store=InMemoryExecutionSessionStore())

    def frame(sequence):
        return json.dumps({"type": "text", "delta": str(sequence), "sequence": sequence}) + "\n"

    await value._output(frame(1) + frame(2))
    await value._output(frame(1) + frame(2))
    assert value.queue.qsize() == 2
    with pytest.raises(WorkerDisconnected, match="gap"):
        await value._output(frame(4))
    assert value.output_sequence == 2
