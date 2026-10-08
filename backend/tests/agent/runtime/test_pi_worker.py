"""Real pinned Pi + Docker. The model is a deterministic protocol fixture."""

import asyncio
import json
from uuid import uuid4

import pytest

from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore
from tests.agent.runtime.workspace_assertions import recovery_file

pytestmark = pytest.mark.integration


def config(**updates):
    return {
        "model": "fixture",
        "max_tokens": 4096,
        "context_window": 131072,
        "readonly": False,
        "prompt": "Complete the requested file task.",
        "workspace": {
            "object_format": "sha1",
            "target_ref": "refs/heads/main",
            "base_oid": None,
            "readonly": False,
        },
        **updates,
    }


async def completion(worker, frame, *, text=None, call=None):
    delta = (
        {"role": "assistant", "content": text}
        if text
        else {
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_fixture",
                    "type": "function",
                    "function": {"name": call[0], "arguments": json.dumps(call[1])},
                }
            ],
        }
    )
    chunk = {
        "id": "model_fixture",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fixture",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    await worker.send({"type": "model_chunk", "id": frame["id"], "frame": chunk})
    chunk["choices"] = [
        {"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}
    ]
    chunk["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    await worker.send({"type": "model_chunk", "id": frame["id"], "frame": chunk})
    await worker.send({"type": "model_end", "id": frame["id"]})


@pytest.mark.asyncio
async def test_real_pi_file_tool_and_checkpoint():
    worker = PiWorker(
        str(uuid4()), "fixture", store=InMemoryExecutionSessionStore(), provider="docker"
    )
    frames, models, checkpoint = [], 0, None
    try:
        await worker.create()
        await worker.start(config())
        async with asyncio.timeout(50):
            while True:
                frame = await worker.receive()
                frames.append(frame["type"])
                if frame["type"] == "checkpoint":
                    checkpoint = frame["checkpoint"]
                    await worker.send({"type": "reply", "id": frame["id"]})
                elif frame["type"] == "model_request":
                    models += 1
                    await completion(
                        worker, frame, call=("write", {"path": "result.txt", "content": "saved"})
                    ) if models == 1 else await completion(worker, frame, text="Done")
                elif frame["type"] == "tool_start":
                    assert frame["name"] == "write"
                    await worker.send({"type": "reply", "id": frame["id"], "allow": True})
                elif frame["type"] == "tool_end":
                    assert "files" not in frame and "git" not in frame
                    recovery = await worker.snapshot()
                    assert recovery_file({"recovery": recovery}, "result.txt") == b"saved"
                    await worker.send({"type": "reply", "id": frame["id"]})
                elif frame["type"] == "finished":
                    assert not frame.get("error"), frame
                    break
                elif frame["type"] in {"failed", "disconnected"}:
                    pytest.fail(str(frame))
        assert {"ready", "tool_start", "tool_end", "text", "finished"} <= set(frames)
        assert any(e.get("message", {}).get("role") == "toolResult" for e in checkpoint["entries"])
        assert "files" not in checkpoint and "git" not in checkpoint
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,arguments,expected",
    [
        ("read", {"path": "../../etc/passwd"}, "outside assigned workspace"),
        (
            "bash",
            {
                "command": """node -e 'fetch("http://example.com", {signal: AbortSignal.timeout(1000)}).then(()=>console.log("BAD_NETWORK")).catch(()=>console.log("EGRESS_DENIED"))' """
            },
            "EGRESS_DENIED",
        ),
        (
            "bash",
            {"command": "env | cut -d= -f1; test ! -e /Users && echo HOST_NOT_MOUNTED"},
            "HOST_NOT_MOUNTED",
        ),
    ],
)
async def test_real_pi_isolation(tool, arguments, expected):
    worker = PiWorker(
        str(uuid4()), "fixture", store=InMemoryExecutionSessionStore(), provider="docker"
    )
    count, content = 0, ""
    try:
        await worker.create()
        await worker.start(config())
        async with asyncio.timeout(30):
            while True:
                frame = await worker.receive()
                if frame["type"] == "model_request":
                    count += 1
                    if count == 1:
                        await completion(worker, frame, call=(tool, arguments))
                    else:
                        content = json.dumps(frame["request"]["messages"])
                        await completion(worker, frame, text="Checked")
                elif frame["type"] == "checkpoint":
                    await worker.send({"type": "reply", "id": frame["id"]})
                elif frame["type"] == "tool_start":
                    await worker.send({"type": "reply", "id": frame["id"], "allow": True})
                elif frame["type"] == "tool_end":
                    await worker.send({"type": "reply", "id": frame["id"]})
                elif frame["type"] == "finished":
                    break
                elif frame["type"] in {"failed", "disconnected"}:
                    pytest.fail(str(frame))
        assert expected in content
        assert "BAD_NETWORK" not in content.split("tool_call_id")[-1]
        for secret in (
            "SUPABASE_KEY",
            "OPENROUTER_API_KEY",
            "E2B_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert secret not in content
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_provider_snapshot_preserves_partial_tool_write_before_cleanup():
    worker = PiWorker(
        str(uuid4()), "fixture", store=InMemoryExecutionSessionStore(), provider="docker"
    )
    try:
        await worker.create()
        await worker.start(config())
        async with asyncio.timeout(20):
            while True:
                frame = await worker.receive()
                if frame["type"] == "checkpoint":
                    await worker.send({"type": "reply", "id": frame["id"]})
                elif frame["type"] == "model_request":
                    await completion(
                        worker,
                        frame,
                        call=("bash", {"command": "echo started > partial.txt; sleep 600"}),
                    )
                elif frame["type"] == "tool_start":
                    await worker.send({"type": "reply", "id": frame["id"], "allow": True})
                    await asyncio.sleep(1)
                    await worker.control("stop")
                    snapshot = await worker.snapshot()
                    assert recovery_file({"recovery": snapshot}, "partial.txt") == b"started\n"
                    later = await worker.snapshot()
                    assert recovery_file({"recovery": later}, "partial.txt") == b"started\n"
                    break
                elif frame["type"] in {"failed", "disconnected"}:
                    pytest.fail(str(frame))
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_full_pi_branches_compaction_restore_from_manifest(submitted):
    from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints

    checkpoint_store = Checkpoints()
    run = submitted[1]
    stamp = "2026-10-06T00:00:00.000Z"

    def entry(identity, parent, kind, **values):
        return {"id": identity, "parentId": parent, "timestamp": stamp, "type": kind, **values}

    entries = [
        {
            "type": "session",
            "version": 3,
            "id": str(uuid4()),
            "timestamp": stamp,
            "cwd": "/workspace",
        },
        entry(
            "old",
            None,
            "message",
            message={"role": "user", "content": "earlier instruction", "timestamp": 1},
        ),
        entry(
            "unused",
            "old",
            "message",
            message={"role": "user", "content": "INACTIVE_BRANCH", "timestamp": 2},
        ),
        entry(
            "keep",
            "old",
            "message",
            message={"role": "user", "content": "retained request", "timestamp": 3},
        ),
        entry(
            "compact",
            "keep",
            "compaction",
            summary="COMPACTION_MEMORY",
            firstKeptEntryId="keep",
            tokensBefore=1000,
        ),
        entry("branch", "compact", "branch_summary", fromId="unused", summary="BRANCH_MEMORY"),
    ]
    saved = {
        "version": 2,
        "pi_version": "0.85.1",
        "entries": entries,
        "leaf_id": "branch",
    }
    for _iteration in range(2):
        worker = PiWorker(
            str(uuid4()), "fixture", store=InMemoryExecutionSessionStore(), provider="docker"
        )
        latest = None
        try:
            await worker.create()
            await worker.start(config(**saved, prompt="Continue with the saved context"))
            async with asyncio.timeout(30):
                while True:
                    frame = await worker.receive()
                    if frame["type"] == "checkpoint":
                        latest = frame["checkpoint"]
                        await worker.send({"type": "reply", "id": frame["id"]})
                    elif frame["type"] == "model_request":
                        rendered = json.dumps(frame["request"]["messages"])
                        assert "COMPACTION_MEMORY" in rendered
                        assert "BRANCH_MEMORY" in rendered
                        assert "INACTIVE_BRANCH" not in rendered
                        await completion(worker, frame, text="Restored context")
                    elif frame["type"] == "finished":
                        break
                    elif frame["type"] in {"failed", "disconnected"}:
                        pytest.fail(str(frame))
            assert all(
                any(e.get("id") == original["id"] for e in latest["entries"])
                for original in entries
            )
            manifest = await checkpoint_store.save(run, latest)
        finally:
            await worker.stop()
        saved = await checkpoint_store.load(run, manifest)
