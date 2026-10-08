"""Real provider retirement and cold reconstruction from canonical Git."""

import asyncio

import pytest

from src.platform.access.adapters.agent.runtime.workspace import reap_workspaces
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from tests.agent.runtime.test_supervisor import (
    BashModel,
    approve_to_completion,
    native_write,
    next_run,
    read_case,
)
from tests.agent.runtime.test_supervisor import (
    prepared as prepared_fixture,
)

prepared = prepared_fixture
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_wakeup_fetches_intervening_cloud_commit_without_replacing_sandbox(prepared):
    case = prepared
    first = case.supervisor()
    result = await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert result["state"] == "succeeded"
    resource = first.worker.resource["resource_id"]
    await native_write(case, "human.md", b"human edit while sandbox paused")
    case.run = await next_run(case, session_id=result["session_id"])
    second = case.supervisor(
        model=BashModel(
            'test "$(cat human.md)" = "human edit while sandbox paused"\n'
            'test "$(cat result.txt)" = "durable result"\n'
            'git fsck --full\nprintf "agent continuation" > continuation.md'
        )
    )
    result = await approve_to_completion(case, asyncio.create_task(second.run_claim(case.run)))
    assert result["state"] == "succeeded", result
    assert second.worker.resource["resource_id"] == resource
    assert second.worker.reused and second.worker.restore_point is None
    assert read_case(case, "human.md") == b"human edit while sandbox paused"
    assert read_case(case, "continuation.md") == b"agent continuation"


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["expiry", "provider_missing"])
async def test_retired_or_missing_workspace_rebuilds_with_new_resource_identity(prepared, cause):
    case = prepared
    await native_write(case, "base.md", b"historical material")
    first = case.supervisor()
    result = await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert result["state"] == "succeeded"
    old = case.postgres.row(
        f"SELECT * FROM agent_session_workspaces WHERE session_id='{result['session_id']}'"
    )
    assert old["state"] == "paused"
    if cause == "expiry":
        case.postgres.sql(
            f"UPDATE agent_session_workspaces SET retire_after=clock_timestamp()-interval '1 second' WHERE session_id='{result['session_id']}'"
        )
        assert await reap_workspaces(case.repo, PiWorker) == 1
        assert await reap_workspaces(case.repo, PiWorker) == 0
    else:
        await PiWorker.cleanup(old["resource"])
    case.run = await next_run(case, session_id=result["session_id"])
    second = case.supervisor(
        model=BashModel(
            'test "$(cat result.txt)" = "durable result"\n'
            'test "$(git show HEAD^:base.md)" = "historical material"\n'
            'git fsck --full\nprintf "after recovery" > recovered.md'
        )
    )
    result = await approve_to_completion(case, asyncio.create_task(second.run_claim(case.run)))
    assert result["state"] == "succeeded", result
    new = case.postgres.row(
        f"SELECT * FROM agent_session_workspaces WHERE session_id='{result['session_id']}'"
    )
    assert new["generation"] != old["generation"]
    assert new["resource"]["resource_id"] != old["resource"]["resource_id"]
    assert new["state"] == "paused"
    # A duplicate old provider cleanup cannot damage the new allocation.
    await PiWorker.cleanup(old["resource"])
    assert read_case(case, "recovered.md") == b"after recovery"
