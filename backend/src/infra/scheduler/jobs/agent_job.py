"""Scheduled Agents submit the same durable runs as interactive clients."""

import asyncio
import logging
from uuid import uuid4

from src.platform.access.adapters.agent.runtime.models import SubmitRun
from src.platform.access.adapters.agent.service import AgentService
from src.platform.access.surface_repository import AccessSurfaceRepository
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import redacted_project_ref

logger = logging.getLogger(__name__)


async def _execute_agent_task_async(agent_id: str) -> dict:
    from src.infra.supabase.client import SupabaseClient
    from src.platform.authorization.factory import build_authorization_service

    client = SupabaseClient().client
    surface = await asyncio.to_thread(
        AccessSurfaceRepository(client).get_agent_with_project, agent_id
    )
    if surface is None:
        return {"status": "failed", "error": "Agent not found"}
    if surface.get("status", "active") != "active":
        return {"status": "skipped", "reason": "agent_paused"}
    user_id = surface.get("created_by") or (surface.get("project") or {}).get("created_by")
    if not user_id:
        return {"status": "failed", "error": "Agent has no associated user"}
    try:
        authorized = await asyncio.to_thread(
            build_authorization_service().allows,
            surface["project_id"],
            user_id,
            ProjectAction.AGENT_RUN,
        )
    except Exception:
        logger.warning(
            "scheduled_agent_principal_check_failed",
            extra={"project_ref": redacted_project_ref(surface["project_id"])},
        )
        return {"status": "failed", "error": "Principal access check failed"}
    if not authorized:
        return {"status": "failed", "error": "principal_invalid"}
    request_id = str(uuid4())
    config = surface.get("config") or {}
    try:
        service = AgentService()
        result = await asyncio.to_thread(
            service.submit,
            user_id,
            SubmitRun(
                project_id=surface["project_id"],
                agent_id=agent_id,
                request_id=request_id,
                prompt=config.get("task_content") or "",
            ),
        )
        # The run is the durable execution log. A scheduler process may exit
        # immediately after this receipt without interrupting the Agent.
        return {"status": "accepted", "run_id": result["id"], "session_id": result["session_id"]}
    except Exception:
        logger.exception("scheduled_agent_submission_failed", extra={"agent_id": agent_id})
        return {"status": "failed", "error": "Agent submission failed"}


def execute_agent_task(agent_id: str):
    return asyncio.run(_execute_agent_task_async(agent_id))
