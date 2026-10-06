"""An approval wait cannot occupy the entire standalone worker."""

import asyncio
from contextlib import suppress
from uuid import uuid4

import pytest

from src.config import settings
from src.platform.access.adapters.agent.runtime import worker
from src.platform.access.adapters.agent.runtime.models import SubmitRun
from tests.agent.runtime.test_supervisor import ModelFixture
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture

prepared = prepared_fixture
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_dispatcher_runs_other_session_while_one_waits_approval(
    prepared, services, monkeypatch
):
    case = prepared
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    second = case.service.submit(
        case.user,
        SubmitRun(
            project_id=case.project,
            agent_id=case.agent,
            request_id=uuid4(),
            prompt="Second independent session",
        ),
    )
    monkeypatch.setattr(settings, "SANDBOX_TYPE", "docker")
    monkeypatch.setattr(settings, "CLOUD_AGENT_CONCURRENCY", 2)
    monkeypatch.setattr(worker, "build_worker_version_engine_container", lambda **_: services[2])
    monkeypatch.setattr(worker, "get_inference_service", lambda *_: ModelFixture())
    task = asyncio.create_task(worker.serve())
    try:
        async with asyncio.timeout(40):
            while True:
                states = [case.repo.get(key)["state"] for key in (case.run["id"], second["id"])]
                if set(states) == {"waiting_approval", "succeeded"}:
                    break
                assert not task.done()
                await asyncio.sleep(0.1)
        waiting = next(
            key
            for key in (case.run["id"], second["id"])
            if case.repo.get(key)["state"] == "waiting_approval"
        )
        case.service.command(case.user, waiting, "stop")
        async with asyncio.timeout(20):
            while case.repo.get(waiting)["state"] != "stopped":
                await asyncio.sleep(0.1)
        assert not case.repo.executions(second["id"])
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        from src.platform.scope_sandbox.execution.pi_worker import PiWorker

        for execution in case.repo.executions(second["id"]):
            if execution["resource"]:
                await PiWorker.cleanup(execution["resource"])
