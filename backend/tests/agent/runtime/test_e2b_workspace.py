"""Opt-in real E2B snapshot contract; does not use any hosted application database."""

import asyncio
import os
from uuid import uuid4

import pytest

from src.config import settings
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore
from tests.agent.runtime.test_pi_worker import completion, config
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture

prepared = prepared_fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("CLOUD_AGENT_TEST_E2B") != "1",
        reason="Explicit E2B test template and credentials required",
    ),
]


async def test_e2b_snapshot_survives_source_deletion_and_rebinds_controller():
    from e2b import AsyncSandbox

    assert settings.CLOUD_AGENT_E2B_TEMPLATE and settings.E2B_API_KEY
    project = str(uuid4())
    workers, snapshots = [], set()
    recovery = None
    try:
        for attempt in range(2):
            worker = PiWorker(
                str(uuid4()), project, provider="e2b", store=InMemoryExecutionSessionStore()
            )
            workers.append(worker)
            worker.restore_point = recovery
            await worker.create()
            await worker.start(config(resume_workspace=bool(recovery)))
            snapshots.add(worker.recovery["id"])
            requests = 0
            async with asyncio.timeout(180):
                while True:
                    frame = await worker.receive()
                    if frame["type"] == "model_request":
                        requests += 1
                        if requests == 1:
                            await completion(
                                worker,
                                frame,
                                call=(
                                    "write",
                                    {"path": "恢复.md", "content": "survives source deletion"},
                                )
                                if attempt == 0
                                else ("read", {"path": "恢复.md"}),
                            )
                        else:
                            assert (
                                "survives source deletion" in str(frame["request"]) or attempt == 0
                            )
                            await completion(worker, frame, text="Restored")
                    elif frame["type"] == "tool_start":
                        await worker.send({"type": "reply", "id": frame["id"], "allow": True})
                    elif frame["type"] == "tool_end":
                        if attempt == 0:
                            recovery = await worker.snapshot()
                            snapshots.add(recovery["id"])
                        else:
                            assert "survives source deletion" in str(frame["result"])
                        await worker.send({"type": "reply", "id": frame["id"]})
                    elif frame["type"] == "checkpoint":
                        assert (
                            "files" not in frame["checkpoint"] and "git" not in frame["checkpoint"]
                        )
                        await worker.send({"type": "reply", "id": frame["id"]})
                    elif frame["type"] == "finished":
                        assert not frame.get("error"), frame
                        break
                    elif frame["type"] in {"failed", "disconnected"}:
                        pytest.fail(str(frame))
            await worker.stop()  # The next attempt must work after source deletion.
    finally:
        for worker in workers:
            await worker.stop()
        for snapshot in snapshots:
            await AsyncSandbox.delete_snapshot(snapshot, api_key=settings.E2B_API_KEY)


@pytest.mark.usefixtures("prepared")
async def test_e2b_stock_git_fetch_commit_push_and_clean_next_turn(prepared, monkeypatch):
    from e2b import AsyncSandbox

    from src.platform.access.adapters.agent.runtime.workspace import reap_workspaces
    from tests.agent.runtime.test_supervisor import (
        BashModel,
        approve_to_completion,
        native_write,
        next_run,
        read_case,
    )

    case = prepared
    await native_write(case, "base.md", b"cloud history")
    snapshots, workers = set(), []
    take_snapshot = PiWorker.snapshot

    async def record_snapshot(worker):
        point = await take_snapshot(worker)
        snapshots.add(point["id"])
        return point

    monkeypatch.setattr(PiWorker, "snapshot", record_snapshot)

    def factory(execution, project):
        worker = PiWorker(execution, project, provider="e2b", store=InMemoryExecutionSessionStore())
        workers.append(worker)
        return worker

    try:
        original_resource = None
        for attempt in range(3):
            supervisor = case.supervisor(
                model=BashModel(
                    'test -f base.md; printf "saved in E2B" > cloud.md'
                    if attempt == 0
                    else 'test "$(cat cloud.md)" = "saved in E2B"; '
                    'test "$(cat human.md)" = "external cloud change"; git fsck --full'
                )
            )
            supervisor.worker_factory = factory
            result = await approve_to_completion(
                case, asyncio.create_task(supervisor.run_claim(case.run)), timeout=240
            )
            assert result["state"] == "succeeded", result
            assert result["publication"]["status"] == (
                "committed" if attempt == 0 else "no_changes"
            )
            assert read_case(case, "cloud.md") == b"saved in E2B"
            workspace = case.postgres.row(
                f"SELECT *,extract(epoch FROM (retire_after-idle_since)) AS idle_seconds "
                f"FROM agent_session_workspaces WHERE session_id='{result['session_id']}'"
            )
            assert workspace["state"] == "paused"
            assert 21599 <= float(workspace["idle_seconds"]) <= 21601
            resource = supervisor.worker.resource["resource_id"]
            info = await AsyncSandbox.get_info(resource, api_key=settings.E2B_API_KEY)
            assert info.state == "paused"
            if attempt == 0:
                original_resource = resource
                await native_write(case, "human.md", b"external cloud change")
            elif attempt == 1:
                assert resource == original_resource
                assert supervisor.worker.reused and supervisor.worker.restore_point is None
                # Own scratch metadata advances the deadline; no six-hour sleep
                # and no mutation of any hosted application database are needed.
                case.postgres.sql(
                    "UPDATE agent_session_workspaces SET retire_after=clock_timestamp()-interval '1 second' "
                    f"WHERE session_id='{result['session_id']}'"
                )
                assert await reap_workspaces(case.repo, PiWorker) == 1
                assert await reap_workspaces(case.repo, PiWorker) == 0
            else:
                assert resource != original_resource
                assert not supervisor.worker.reused
            if attempt < 2:
                case.run = await next_run(case, session_id=result["session_id"])
    finally:
        for worker in workers:
            await worker.stop()
        for snapshot in snapshots:
            await AsyncSandbox.delete_snapshot(snapshot, api_key=settings.E2B_API_KEY)
