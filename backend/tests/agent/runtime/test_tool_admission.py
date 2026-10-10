"""Tool rejections are durable outcomes; exercise the deployed Pi protocol unchanged."""

import asyncio
import json
import os
from uuid import uuid4

import pytest

from src.config import settings
from src.platform.managed_ai.contracts import InferenceRun, ModelChunk
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore
from tests.agent.runtime.test_supervisor import (
    ModelFixture,
    approve_to_completion,
    native_write,
    next_run,
    read_case,
    wait_for_approval,
)
from tests.agent.runtime.test_supervisor import (
    prepared as prepared_fixture,
)

prepared = prepared_fixture
pytestmark = pytest.mark.integration

INCIDENT_COMMAND = (
    "cd /workspace/repo && git log --oneline -15 2>/dev/null; "
    'echo "---STATUS---"; git status 2>/dev/null | head -40'
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        "docker",
        pytest.param(
            "e2b",
            marks=pytest.mark.skipif(
                os.getenv("CLOUD_AGENT_TEST_E2B") != "1", reason="Explicit E2B credentials required"
            ),
        ),
    ],
)
async def test_question_git_inspection_and_tool_error_complete_then_resume(
    prepared, monkeypatch, provider
):
    """The incident's command, a failed tool, publication and a warm next turn."""
    case = prepared
    await native_write(case, "base.md", b"synthetic fixture")
    case.postgres.sql(f"UPDATE agent_runs SET prompt='?' WHERE id='{case.run['id']}'")
    case.run = case.repo.get(case.run["id"])
    workers, snapshots = [], set()
    take_snapshot = PiWorker.snapshot

    async def snapshot(worker):
        point = await take_snapshot(worker)
        if provider == "e2b":
            snapshots.add(point["id"])
        return point

    monkeypatch.setattr(PiWorker, "snapshot", snapshot)

    def factory(execution, project):
        worker = PiWorker(
            execution, project, provider=provider, store=InMemoryExecutionSessionStore()
        )
        workers.append(worker)
        return worker

    try:
        for turn in range(2):
            steps = [("bash", {"command": INCIDENT_COMMAND}, "---STATUS---")]
            if turn == 0:
                steps += [
                    ("bash", {"command": "printf 'synthetic failure' >&2; exit 7"}, "7"),
                    ("write", {"path": "recovered.txt", "content": "recovered"}, "recovered.txt"),
                ]
            model = RecoveringToolModel(steps)
            supervisor = case.supervisor(model=model)
            supervisor.worker_factory = factory
            result = await approve_to_completion(
                case, asyncio.create_task(supervisor.run_claim(case.run)), timeout=240
            )
            assert result["state"] == "succeeded", result
            assert result["snapshot"]["text"] == "Recovered and completed."
            assert result["publication"]["status"] == ("committed" if turn == 0 else "no_changes")
            tools = case.repo.tools(result["id"])
            assert len(tools) == len(steps)
            assert model.calls == len(steps) + 1
            assert not tools[0]["result"]["pi_result"].get("isError")
            if turn == 0:
                assert tools[1]["result"]["pi_result"]["isError"] is True
            assert read_case(case, "recovered.txt") == b"recovered"
            operations = supervisor.metrics.report()["operations"]
            assert operations["POST rpc/agent_run_complete_tool"] == len(steps)
            assert operations["POST rpc/agent_run_begin_model"] == len(steps) + 1
            if turn:
                assert supervisor.worker.reused
                assert workers[0].resource["resource_id"] == workers[1].resource["resource_id"]
            else:
                case.run = await next_run(case, session_id=result["session_id"])
    finally:
        for worker in workers:
            await worker.stop()
        if provider == "e2b":
            from e2b import AsyncSandbox

            for identity in snapshots:
                await AsyncSandbox.delete_snapshot(identity, api_key=settings.E2B_API_KEY)


class RecoveringToolModel(ModelFixture):
    """Real Pi must deliver every tool outcome back to the next model turn."""

    def __init__(self, steps):
        super().__init__()
        self.steps = steps

    async def completion(self, user, request, body):
        index = self.calls
        if index:
            tools = [m for m in body.model_dump()["messages"] if m["role"] == "tool"]
            assert self.steps[index - 1][2] in json.dumps(tools[-1]), tools[-1]
        result = await super().completion(user, request, body)
        has_tool = index < len(self.steps)

        async def events():
            async for event in result.events:
                if isinstance(event, ModelChunk):
                    for choice in event.frame["choices"]:
                        if choice["finish_reason"] is not None:
                            choice["finish_reason"] = "tool_calls" if has_tool else "stop"
                        else:
                            name, arguments, _expected = (
                                self.steps[index] if has_tool else (None, None, None)
                            )
                            choice["delta"] = (
                                {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": f"recover-{index}",
                                            "type": "function",
                                            "function": {
                                                "name": name,
                                                "arguments": json.dumps(arguments),
                                            },
                                        }
                                    ],
                                }
                                if has_tool
                                else {"role": "assistant", "content": "Recovered and completed."}
                            )
                yield event

        return InferenceRun(str(uuid4()), events(), result.aclose)


@pytest.mark.asyncio
async def test_declined_tool_continues_without_executing_or_losing_decision(prepared):
    case = prepared
    await native_write(case, "base.md", b"synthetic data")
    model = RecoveringToolModel(
        [
            ("write", {"path": "declined.txt", "content": "never"}, "declined"),
            ("ls", {"path": "."}, "base.md"),
        ]
    )
    task = asyncio.create_task(case.supervisor(model=model).run_claim(case.run))
    tool = await wait_for_approval(case, task)
    decision = str(uuid4())
    for _ in range(2):
        await asyncio.to_thread(
            case.service.command,
            case.user,
            case.run["id"],
            "approve",
            call=tool["call_id"],
            decision=decision,
            allow=False,
        )
    result = await approve_to_completion(case, task)
    assert result["state"] == "succeeded", result
    receipt = case.repo.tools(case.run["id"])[0]
    assert receipt["state"] == "rejected"
    assert receipt["decision_id"] == decision
    assert model.calls == 3
    with pytest.raises(FileNotFoundError):
        read_case(case, "declined.txt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,readonly,code",
    [
        ("write", True, "tool_permission_denied"),
        ("browser", False, "tool_unavailable"),
        ("write", False, "tool_invalid_input"),
    ],
)
async def test_denial_is_durable_before_reply_and_replays_with_bounded_queries(
    prepared, name, readonly, code
):
    from unittest.mock import AsyncMock

    from src.infra.supabase.instrumentation import DatabaseTrace, database_trace

    case = prepared
    case.postgres.sql(
        f"UPDATE agent_runs SET policy=jsonb_set(policy,'{{readonly}}','{json.dumps(readonly)}') WHERE id='{case.run['id']}'"
    )
    supervisor = case.supervisor()
    supervisor.run = case.repo.get(case.run["id"])
    supervisor.value = {}
    supervisor.worker = AsyncMock()
    frame = {
        "id": "request",
        "call_id": "denied",
        "name": name,
        "input": {"path": "no.txt"},
        "checkpoint": {"version": 1, "pi_version": "0.85.1", "entries": [], "leaf_id": None},
    }
    if code == "tool_invalid_input":
        frame["error_result"] = {
            "isError": True,
            "content": [{"type": "text", "text": "Invalid path argument"}],
        }
    trace = DatabaseTrace("denial")
    with database_trace(trace):
        await supervisor.tool_start(frame)
    assert trace.report()["operations"] == {
        "POST rpc/agent_run_begin_tool": 1,
        "POST rpc/agent_run_complete_tool": 1,
    }
    receipt = case.repo.tools(case.run["id"])[0]
    result = supervisor.worker.send.await_args.args[0]["result"]
    assert result == receipt["result"]["pi_result"]
    assert result["details"]["code"] == code
    assert result["isError"] is True
    assert case.repo.get(case.run["id"])["state"] == "running"
    replay = DatabaseTrace("replay")
    with database_trace(replay):
        await supervisor.tool_start({**frame, "id": "retry"})
    assert supervisor.worker.send.await_args.args[0]["result"] == result
    assert replay.report()["operations"] == {"POST rpc/agent_run_begin_tool": 1}
    assert len(case.repo.tools(case.run["id"])) == 1


@pytest.mark.asyncio
async def test_declined_continuation_is_idempotent_and_uses_bounded_database_commands(prepared):
    from unittest.mock import AsyncMock

    from src.infra.supabase.instrumentation import DatabaseTrace, database_trace

    case = prepared
    frame = {
        "id": "request",
        "call_id": "declined",
        "name": "write",
        "input": {"path": "no.txt"},
        "checkpoint": {"version": 1, "pi_version": "0.85.1", "entries": [], "leaf_id": None},
    }
    case.repo.begin_tool(case.run, frame, checkpoint={}, mutation=True)
    decision = str(uuid4())
    await asyncio.to_thread(
        case.service.command,
        case.user,
        case.run["id"],
        "approve",
        call="declined",
        decision=decision,
        allow=False,
    )
    supervisor = case.supervisor()
    supervisor.run, supervisor.value, supervisor.worker = case.run, {}, AsyncMock()
    sequence = None
    for _ in range(2):
        trace = DatabaseTrace("declined-continuation")
        with database_trace(trace):
            await supervisor.tool_start(frame)
        assert trace.report()["operations"] == {
            "POST rpc/agent_run_begin_tool": 1,
            "POST rpc/agent_run_append_batch": 1,
        }
        assert supervisor.run["state"] == "running"
        if sequence is not None:
            assert supervisor.run["sequence"] == sequence
        sequence = supervisor.run["sequence"]
        result = supervisor.worker.send.await_args.args[0]["result"]
        assert result["details"]["code"] == "tool_approval_denied"
    receipt = case.repo.tools(case.run["id"])[0]
    assert receipt["state"] == "rejected" and receipt["decision_id"] == decision
