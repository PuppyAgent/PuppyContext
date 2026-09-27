"""
Agent execution job for scheduled tasks.

This module contains the function that APScheduler calls when a scheduled
agent task needs to run.
"""

import asyncio
from datetime import UTC, datetime

from src.platform.authorization.service import redacted_project_ref
from src.utils.logger import log_error, log_info


async def _execute_agent_task_async(agent_id: str) -> dict:
    """
    Execute a scheduled agent task asynchronously.

    This function:
    1. Loads the agent configuration from database
    2. Creates an execution log entry
    3. Runs the agent task using AgentService.execute_task_sync()
    4. Updates the execution log with results

    Args:
        agent_id: The unique ID of the agent to execute

    Returns:
        dict with execution results
    """
    from src.platform.access.adapters.agent.config.service import AgentConfigService
    from src.platform.access.adapters.agent.service import AgentService
    from src.infra.supabase.client import SupabaseClient
    from src.platform.scope_sandbox.execution.service import SandboxService

    started_at = datetime.now(UTC)
    execution_id: str | None = None

    log_info(f"🚀 Starting scheduled execution for agent {agent_id}")

    try:
        db_client = SupabaseClient().client

        from src.platform.access.surface_repository import AccessSurfaceRepository

        agent = AccessSurfaceRepository(db_client).get_agent_with_project(agent_id)

        if not agent:
            log_error(f"Agent {agent_id} not found")
            return {"status": "failed", "error": "Agent not found"}

        # Pause is a hard "skip this scheduled tick" toggle that mirrors
        # the access-surface pause used by chat / CLI access (see
        # ``chat/service._enforce_agent_not_paused``). Reporting
        # ``skipped`` instead of ``failed`` keeps the agent's
        # execution-log history clean — a paused agent should not
        # accumulate failure rows.
        if agent.get("status") == "paused":
            log_info(f"⏸  Agent {agent_id} is paused; skipping scheduled run")
            return {"status": "skipped", "reason": "agent_paused"}

        config = agent.get("config") or {}
        trigger = config.get("trigger") or {}
        project_data = agent.get("project")
        project_id = agent.get("project_id")
        # SECURITY (M-3): Pick the principal in priority order:
        #   1. The access-surface owner (the user who CREATED the agent) —
        #      this is the natural impersonation target.
        #   2. The project creator — only for rows created before surface
        #      ownership was consistently recorded.
        # We then RE-VERIFY that this user still has project access at
        # execution time. Persisted IDs are stale: the user may have been
        # removed from the project / org since the job was scheduled.
        user_id = agent.get("created_by") or (
            project_data.get("created_by") if project_data else None
        )
        if not user_id:
            log_error(f"Agent {agent_id} has no associated user")
            return {"status": "failed", "error": "Agent has no associated user"}

        try:
            from src.platform.authorization.factory import build_authorization_service
            from src.platform.authorization.models import ProjectAction

            principal_allowed = build_authorization_service().allows(
                project_id, user_id, ProjectAction.AGENT_RUN
            )
        except Exception as verify_err:
            log_error(
                "Scheduled Agent principal access check failed "
                f"project_ref={redacted_project_ref(project_id)} "
                f"error_type={type(verify_err).__name__}"
            )
            return {
                "status": "failed",
                "error": "Principal access check failed",
            }
        if not principal_allowed:
            log_error(
                "Scheduled Agent principal no longer has Project access; "
                f"project_ref={redacted_project_ref(project_id)}"
            )
            return {
                "status": "failed",
                "error": "principal_invalid: scheduled job creator no longer has project access",
            }

        from src.platform.project.readiness import ProjectReadinessService

        readiness = ProjectReadinessService().resolve(project_id)
        if not readiness.claude_ready:
            log_error(f"⛔ Agent {agent_id}: Project Agent readiness blockers={readiness.blockers}")
            return {
                "status": "failed",
                "error": "project_agent_not_ready",
                "blockers": readiness.blockers,
            }

        agent_name = config.get("name", "Unknown")
        task_content = config.get("task_content", "")

        log_info(f"📋 Agent loaded: {agent_name} (type: {config.get('type')})")
        log_info(
            f"📋 Task content: {task_content[:100]}..."
            if len(task_content) > 100
            else f"📋 Task content: {task_content}"
        )

        # 2. Create execution log entry
        execution_data = {
            "agent_id": agent_id,
            "trigger_type": "cron",
            "trigger_source": "scheduler",
            "status": "running",
            "started_at": started_at.isoformat(),
            "input_snapshot": {
                "task_content": task_content,
                "trigger_config": trigger.get("config"),
            },
        }

        log_result = db_client.table("agent_execution_logs").insert(execution_data).execute()
        if log_result.data:
            execution_id = log_result.data[0].get("id")
            log_info(f"📝 Created execution log: {execution_id}")

        # 3. Initialize services
        log_info("🔧 Initializing services...")

        # Create service instances
        SupabaseClient()
        agent_service = AgentService()
        agent_config_service = AgentConfigService()
        sandbox_service = SandboxService()

        # 4. Execute the agent task using AgentService
        log_info(f"🤖 Running task for agent '{agent_name}'...")

        async def run_agent():
            return await agent_service.execute_task_sync(
                agent_id=agent_id,
                task_content=task_content,
                user_id=user_id,
                ops=None,
                sandbox_service=sandbox_service,
                agent_config_service=agent_config_service,
            )

        configured_agent = agent_config_service.get_agent(agent_id)
        if configured_agent and configured_agent.bash_accesses:
            # SandboxService meters the complete provider lifetime.
            exec_result = await run_agent()
        else:
            from src.config import settings
            from src.platform.billing.runtime import get_runtime_metering_service

            exec_result = await get_runtime_metering_service().execute(
                audit_context={
                    "source": "schedule_agent",
                    "run_id": (
                        f"schedule-agent:{execution_id}"
                        if execution_id
                        else f"schedule-agent:{agent_id}:{started_at.isoformat()}"
                    ),
                    "project_id": project_id,
                    "user_id": user_id,
                    "maximum_runtime_units": settings.RUNTIME_AGENT_MAX_UNITS,
                    "maximum_runtime_seconds": settings.RUNTIME_AGENT_TIMEOUT_SECONDS,
                },
                operation=run_agent,
            )

        # 5. Update execution log with results
        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)

        if exec_result.get("status") == "success":
            if execution_id:
                db_client.table("agent_execution_logs").update(
                    {
                        "status": "success",
                        "finished_at": finished_at.isoformat(),
                        "duration_ms": duration_ms,
                        "output_summary": exec_result.get("output_summary", "")[:2000],
                        "output_snapshot": {
                            "tool_calls": exec_result.get("tool_calls", []),
                            "updated_nodes": exec_result.get("updated_nodes", []),
                        },
                    }
                ).eq("id", execution_id).execute()

            log_info(f"✅ Agent {agent_id} execution completed in {duration_ms}ms")
            log_info(f"📊 Updated nodes: {exec_result.get('updated_nodes', [])}")
        else:
            error_msg = exec_result.get("error", "Unknown error")
            if execution_id:
                db_client.table("agent_execution_logs").update(
                    {
                        "status": "failed",
                        "finished_at": finished_at.isoformat(),
                        "duration_ms": duration_ms,
                        "error_message": error_msg,
                    }
                ).eq("id", execution_id).execute()

            log_error(f"❌ Agent {agent_id} execution failed: {error_msg}")

        return {
            "status": exec_result.get("status", "failed"),
            "execution_id": execution_id,
            "duration_ms": duration_ms,
            "output_summary": exec_result.get("output_summary", ""),
            "updated_nodes": exec_result.get("updated_nodes", []),
        }

    except Exception as e:
        log_error(f"❌ Agent {agent_id} execution failed: {e}")
        import traceback

        log_error(f"Traceback: {traceback.format_exc()}")

        # Update execution log with failure
        if execution_id:
            try:
                client = SupabaseClient().client
                finished_at = datetime.now(UTC)
                duration_ms = int((finished_at - started_at).total_seconds() * 1000)

                client.table("agent_execution_logs").update(
                    {
                        "status": "failed",
                        "finished_at": finished_at.isoformat(),
                        "duration_ms": duration_ms,
                        "error_message": str(e),
                    }
                ).eq("id", execution_id).execute()
            except Exception as update_error:
                log_error(f"Failed to update execution log: {update_error}")

        return {
            "status": "failed",
            "execution_id": execution_id,
            "error": str(e),
        }


def execute_agent_task(agent_id: str):
    """
    Synchronous wrapper for APScheduler.

    APScheduler calls this function from a ThreadPoolExecutor.
    We create a new event loop to run the async task.
    """
    log_info(f"⏰ Scheduler triggered for agent {agent_id}")

    try:
        # Create new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(_execute_agent_task_async(agent_id))
            return result
        finally:
            loop.close()

    except Exception as e:
        log_error(f"Failed to execute agent task: {e}")
        return {"status": "failed", "error": str(e)}
