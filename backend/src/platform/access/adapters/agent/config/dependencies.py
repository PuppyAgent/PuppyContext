"""
Agent Config Dependencies

FastAPI dependency injection.
"""

from typing import Callable

from fastapi import Depends, HTTPException, Query, status

from src.platform.access.adapters.agent.config.service import AgentConfigService
from src.platform.access.adapters.agent.config.repository import AgentRepository
from src.platform.access.adapters.agent.config.models import Agent
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService


_AGENT_NOT_FOUND = "Agent not found"


def get_agent_repository() -> AgentRepository:
    """Get AgentRepository instance."""
    return AgentRepository()


def get_agent_config_service(
    repo: AgentRepository = Depends(get_agent_repository),
) -> AgentConfigService:
    """Get AgentConfigService instance."""
    return AgentConfigService(repository=repo)


def require_agent_read_query(
    project_id: str = Query(..., description="Project ID (required)"),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> str:
    """Authorize the named Agent read action from a query Project id."""
    authorization.authorize(
        project_id, current_user.user_id, ProjectAction.AGENT_READ
    )
    return project_id


def require_agent_manage_body(
    project_id: str,
    current_user: CurrentUser,
    authorization: AuthorizationService,
) -> str:
    """Authorize the named Agent manage action from a request-body Project id.

    Not a Depends — call directly inside the handler after extracting project_id
    from the validated payload.
    """
    authorization.authorize(
        project_id, current_user.user_id, ProjectAction.AGENT_MANAGE
    )
    return project_id


def require_agent_action(action: ProjectAction) -> Callable[..., Agent]:
    def dependency(
        agent_id: str,
        current_user: CurrentUser = Depends(get_current_user),
        service: AgentConfigService = Depends(get_agent_config_service),
        authorization: AuthorizationService = Depends(get_authorization_service),
    ) -> Agent:
        project_id = service.get_agent_project_id(agent_id)
        if project_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_AGENT_NOT_FOUND,
            )
        authorization.authorize(project_id, current_user.user_id, action)
        if not service.is_visible_to(agent_id, current_user.user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_AGENT_NOT_FOUND,
            )
        agent = service.get_agent(agent_id)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_AGENT_NOT_FOUND,
            )
        return agent

    return dependency


get_verified_agent = require_agent_action(ProjectAction.AGENT_READ)
get_writable_agent = require_agent_action(ProjectAction.AGENT_MANAGE)
get_credential_agent = require_agent_action(ProjectAction.CREDENTIAL_MANAGE)
