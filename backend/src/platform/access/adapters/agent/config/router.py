"""
Agent Config Router

REST API for Agent configuration.
"""

import asyncio
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks

from src.version_engine.bootstrap.dependencies import get_product_operation_adapter
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter

from src.platform.access.adapters.agent.config.service import AgentConfigService
from src.infra.scheduler.service import get_scheduler_service
from src.infra.scheduler.config import scheduler_settings
from src.utils.logger import log_info, log_error
from src.platform.access.adapters.agent.config.dependencies import (
    get_agent_config_service,
    get_verified_agent,
    get_writable_agent,
    require_agent_read_query,
    require_agent_manage_body,
)
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService
from src.platform.access.adapters.agent.config.models import Agent
from src.platform.access.adapters.agent.config.schemas import (
    AgentCreate,
    AgentUpdate,
    AgentOut,
    AgentBashCreate,
    AgentBashUpdate,
    AgentBashOut,
)
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.common_schemas import ApiResponse

router = APIRouter(
    prefix="/agent-config",
    tags=["agent-config"],
    responses={
        404: {"description": "Resource not found"},
        403: {"description": "Access denied"},
        500: {"description": "Internal server error"},
    },
)


def _to_agent_out(
    agent: Agent,
    node_info: Optional[dict[str, dict]] = None,
) -> AgentOut:
    """Convert internal Agent → API AgentOut.

    PERFORMANCE (P-5): node_name / node_type can be inlined here when the
    caller has pre-resolved node metadata in batch (single hash walk for all
    agents in a project), instead of forcing the frontend to issue a
    separate fetchNodeInfoBatch round-trip.
    """
    info = node_info or {}
    bash_out = [
        AgentBashOut(
            id=a.id,
            agent_id=a.agent_id,
            path=a.path,
            readonly=a.readonly,
            node_name=info.get(a.path, {}).get("name"),
            node_type=info.get(a.path, {}).get("type"),
        )
        for a in agent.bash_accesses
    ]

    return AgentOut(
        id=agent.id,
        name=agent.name,
        icon=agent.icon,
        type=agent.type,
        description=agent.description,
        is_default=agent.is_default,
        project_id=agent.project_id,
        mcp_api_key=agent.mcp_api_key,
        mcp_enabled=agent.mcp_enabled,
        mcp_key_last4=agent.mcp_key_last4,
        trigger_type=agent.trigger_type,
        trigger_config=agent.trigger_config,
        task_content=agent.task_content,
        task_path=agent.task_path,
        external_config=agent.external_config,
        status=agent.status,
        created_at=agent.created_at.isoformat(),
        updated_at=agent.updated_at.isoformat(),
        bash_accesses=bash_out,
    )


# ============================================
# Agent CRUD
# ============================================

@router.get(
    "/",
    response_model=ApiResponse[List[AgentOut]],
    summary="List Agents",
    description="Get the list of Agents for a specified project",
)
def list_agents(
    project_id: str = Depends(require_agent_read_query),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    # The named Agent read action is enforced before repository reads.
    # caller is a member of project_id BEFORE any agent data is read.
    # SECURITY (M-1): pass viewer_user_id so private agents owned by other
    # users in the same org are filtered out of the response.
    agents = service.list_agents(project_id, viewer_user_id=current_user.user_id)

    # PERFORMANCE (P-5): batch-resolve every bash_access path once instead of
    # forcing the client to issue a second N-fan-out round trip
    # (fetchNodeInfoBatch).
    paths = {b.path for a in agents for b in a.bash_accesses if b.path is not None}
    node_info: dict[str, dict] = {}
    for p in paths:
        try:
            entry = ops.stat(project_id, p)
        except Exception:
            entry = None
        if entry:
            node_info[p] = {"name": entry.name, "type": entry.type}

    return ApiResponse.success(
        data=[_to_agent_out(a, node_info=node_info) for a in agents],
        message="Agent list retrieved successfully",
    )


@router.get(
    "/default",
    response_model=ApiResponse[AgentOut],
    summary="Get default Agent",
    description="Get the default Agent for a specified project",
)
def get_default_agent(
    project_id: str = Depends(require_agent_read_query),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    # SECURITY (C-2): membership verified via dependency.
    agent = service.get_default_agent(
        project_id, viewer_user_id=current_user.user_id
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No default agent found",
        )
    return ApiResponse.success(
        data=_to_agent_out(agent),
        message="Default Agent retrieved successfully",
    )


@router.get(
    "/{agent_id}",
    response_model=ApiResponse[AgentOut],
    summary="Get Agent details",
    description="Get Agent details by ID",
)
def get_agent(
    agent: Agent = Depends(get_verified_agent),
):
    return ApiResponse.success(
        data=_to_agent_out(agent),
        message="Agent retrieved successfully",
    )


def _sync_scheduler_add(agent_id: str, agent_type: str, trigger_type: str, trigger_config: dict, agent_name: str):
    """Background task to add agent to scheduler."""
    if not scheduler_settings.enabled:
        return
    if agent_type != "schedule" or trigger_type != "cron":
        return

    try:
        scheduler = get_scheduler_service()
        # Run async method in a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                scheduler.add_agent_job(agent_id, trigger_config or {}, agent_name)
            )
        finally:
            loop.close()
        log_info(f"✅ Scheduler synced: added job for agent {agent_id}")
    except Exception as e:
        log_error(f"❌ Failed to sync scheduler for agent {agent_id}: {e}")


def _sync_scheduler_remove(agent_id: str):
    """Background task to remove agent from scheduler."""
    if not scheduler_settings.enabled:
        return

    try:
        scheduler = get_scheduler_service()
        scheduler.remove_agent_job(agent_id)
        log_info(f"✅ Scheduler synced: removed job for agent {agent_id}")
    except Exception as e:
        log_error(f"❌ Failed to remove scheduler job for agent {agent_id}: {e}")


@router.post(
    "/",
    response_model=ApiResponse[AgentOut],
    summary="Create Agent",
    description="Create a new Agent",
    status_code=status.HTTP_201_CREATED,
)
def create_agent(
    payload: AgentCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    if not payload.project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required",
        )

    # SECURITY (C-2): the project_id comes from the request body, so we
    # verify membership manually instead of via Depends.
    require_agent_manage_body(
        payload.project_id, current_user, authorization,
    )

    agent = service.create_agent(
        project_id=payload.project_id,
        name=payload.name,
        icon=payload.icon,
        type=payload.type,
        description=payload.description,
        is_default=False,
        bash_accesses=payload.bash_accesses,
        trigger_type=payload.trigger_type,
        trigger_config=payload.trigger_config,
        task_content=payload.task_content,
        task_path=payload.task_path,
        external_config=payload.external_config,
        owner_user_id=current_user.user_id,
    )

    # Sync with scheduler if this is a schedule agent
    background_tasks.add_task(
        _sync_scheduler_add,
        agent.id,
        payload.type,
        payload.trigger_type or "manual",
        payload.trigger_config or {},
        payload.name,
    )

    return ApiResponse.success(
        data=_to_agent_out(agent),
        message="Agent created successfully",
    )


@router.put(
    "/{agent_id}",
    response_model=ApiResponse[AgentOut],
    summary="Update Agent",
    description="Update Agent information",
)
def update_agent(
    payload: AgentUpdate,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_writable_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    updated = service.update_agent(
        agent_id=agent.id,
        user_id=current_user.user_id,
        name=payload.name,
        icon=payload.icon,
        type=payload.type,
        description=payload.description,
        is_default=payload.is_default,
        # Schedule Agent new fields
        trigger_type=payload.trigger_type,
        trigger_config=payload.trigger_config,
        task_content=payload.task_content,
        task_path=payload.task_path,
        external_config=payload.external_config,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update agent",
        )

    # Sync with scheduler
    new_type = payload.type or agent.type
    new_trigger_type = payload.trigger_type or agent.trigger_type or "manual"
    new_trigger_config = payload.trigger_config if payload.trigger_config is not None else (agent.trigger_config or {})

    if new_type == "schedule" and new_trigger_type == "cron":
        # Add/update job
        background_tasks.add_task(
            _sync_scheduler_add,
            agent.id,
            new_type,
            new_trigger_type,
            new_trigger_config,
            payload.name or agent.name,
        )
    else:
        # Remove job if agent is no longer a schedule agent
        background_tasks.add_task(_sync_scheduler_remove, agent.id)

    return ApiResponse.success(
        data=_to_agent_out(updated),
        message="Agent updated successfully",
    )


@router.delete(
    "/{agent_id}",
    response_model=ApiResponse[None],
    summary="Delete Agent",
    description="Delete an Agent and all its access permissions",
)
def delete_agent(
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_writable_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    agent_id = agent.id
    success = service.delete_agent(agent_id, current_user.user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete agent",
        )

    # Remove from scheduler
    background_tasks.add_task(_sync_scheduler_remove, agent_id)

    return ApiResponse.success(message="Agent deleted successfully")


# ============================================
# AgentBash CRUD (new version)
# ============================================

@router.post(
    "/{agent_id}/bash",
    response_model=ApiResponse[AgentBashOut],
    summary="Add Bash access permission",
    description="Add a new Bash terminal access permission to an Agent",
    status_code=status.HTTP_201_CREATED,
)
def add_bash(
    payload: AgentBashCreate,
    agent: Agent = Depends(get_writable_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    bash = service.add_bash(
        agent_id=agent.id,
        user_id=current_user.user_id,
        path=payload.path,
        readonly=payload.readonly,
    )
    if not bash:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add bash access",
        )
    return ApiResponse.success(
        data=AgentBashOut(
            id=bash.id,
            agent_id=bash.agent_id,
            path=bash.path,
            readonly=bash.readonly,
        ),
        message="Bash access permission added successfully",
    )


@router.put(
    "/{agent_id}/bash/{bash_id}",
    response_model=ApiResponse[AgentBashOut],
    summary="Update Bash access permission",
    description="Update Bash terminal access permission for an Agent",
)
def update_bash(
    bash_id: str,
    payload: AgentBashUpdate,
    agent: Agent = Depends(get_writable_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    bash = service.update_bash(
        bash_id=bash_id,
        user_id=current_user.user_id,
        readonly=payload.readonly,
    )
    if not bash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bash access not found or not authorized",
        )
    return ApiResponse.success(
        data=AgentBashOut(
            id=bash.id,
            agent_id=bash.agent_id,
            path=bash.path,
            readonly=bash.readonly,
        ),
        message="Bash access permission updated successfully",
    )


@router.delete(
    "/{agent_id}/bash/{bash_id}",
    response_model=ApiResponse[None],
    summary="Delete Bash access permission",
    description="Delete a single Bash terminal access permission for an Agent",
)
def remove_bash(
    bash_id: str,
    agent: Agent = Depends(get_writable_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    success = service.remove_bash(bash_id, current_user.user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bash access not found or not authorized",
        )
    return ApiResponse.success(message="Bash access permission deleted successfully")


# ============================================
# Execution History
# ============================================

@router.get(
    "/{agent_id}/executions",
    response_model=ApiResponse[List[dict]],
    summary="Get execution history",
    description="Get Agent execution history records",
)
def get_execution_history(
    agent: Agent = Depends(get_verified_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
    limit: int = 10,
):
    executions = service.get_execution_history(agent.id, limit)
    return ApiResponse.success(
        data=executions,
        message="Execution history retrieved successfully",
    )


@router.put(
    "/{agent_id}/bash",
    response_model=ApiResponse[List[AgentBashOut]],
    summary="Sync Bash access permissions",
    description="Full replacement of all Bash terminal access permissions for an Agent",
)
def sync_bash(
    bash_list: List[AgentBashCreate],
    agent: Agent = Depends(get_writable_agent),
    current_user: CurrentUser = Depends(get_current_user),
    service: AgentConfigService = Depends(get_agent_config_service),
):
    result = service.sync_bash(
        agent_id=agent.id,
        user_id=current_user.user_id,
        bash_list=bash_list,
    )
    return ApiResponse.success(
        data=[
            AgentBashOut(
                id=a.id,
                agent_id=a.agent_id,
                path=a.path,
                readonly=a.readonly,
            )
            for a in result
        ],
        message="Bash access permissions synced successfully",
    )
