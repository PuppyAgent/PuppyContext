"""Run with python -m src.platform.access.adapters.agent.runtime.worker."""

import asyncio
import logging
from uuid import uuid4

from src.platform.access.adapters.agent.runtime.admission import Admission
from src.platform.access.adapters.agent.runtime.publication import Publication
from src.platform.access.adapters.agent.runtime.repository import RunRepository
from src.platform.access.adapters.agent.runtime.runner import RunSupervisor
from src.platform.access.adapters.agent.runtime.tools import BoundTools
from src.platform.access.adapters.agent.runtime.workspace import SessionWorkspace, reap_workspaces
from src.platform.billing.gateway import get_billing_gateway
from src.platform.billing.runtime import get_runtime_metering_service
from src.platform.managed_ai.dependencies import get_inference_service, get_provider_registry
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.version_engine.adapters.git.run_transport import RunGitTransport
from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container

logger = logging.getLogger(__name__)


async def serve():
    from src.config import settings

    repository = RunRepository()
    admission = Admission(repository)
    engine = build_worker_version_engine_container(probe=True)
    publication = Publication(RunGitTransport(engine.repo_manager))
    inference = get_inference_service(get_billing_gateway(), get_provider_registry())
    billing = get_runtime_metering_service()
    bound_tools = BoundTools(admission)
    worker_id = str(uuid4())
    active = set()

    async def supervise(run):
        try:
            await RunSupervisor(
                repository,
                admission,
                publication,
                inference,
                billing,
                bound_tools=bound_tools,
                worker_factory=PiWorker,
                worker_lifecycle=PiWorker,
                workspace=SessionWorkspace(repository),
            ).run_claim(run)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable lease allows another owner to repair a failed cleanup.
            logger.exception("cloud_agent_supervisor_failed", extra={"run_id": run["id"]})

    async def retire_idle():
        while True:
            try:
                await reap_workspaces(repository, PiWorker)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cloud_agent_workspace_reaper_failed")
            await asyncio.sleep(30)

    reaper = asyncio.create_task(retire_idle())
    try:
        while True:
            if len(active) >= settings.CLOUD_AGENT_CONCURRENCY:
                await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                run = await asyncio.to_thread(repository.claim_run, worker=worker_id)
                if run is None:
                    await asyncio.sleep(1)
                    continue
                task = asyncio.create_task(supervise(run))
                active.add(task)
                task.add_done_callback(active.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cloud_agent_dispatch_failed")
                await asyncio.sleep(2)
    finally:
        pending = [*active, reaper]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    from src.utils.logging_setup import setup_logging

    setup_logging()
    asyncio.run(serve())
