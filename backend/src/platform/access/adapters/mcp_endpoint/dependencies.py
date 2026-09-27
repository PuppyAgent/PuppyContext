from typing import Callable

from fastapi import Depends, HTTPException

from src.platform.access.adapters.mcp_endpoint.repository import McpEndpointRepository
from src.platform.access.adapters.mcp_endpoint.service import McpEndpointService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService


def get_mcp_endpoint_repository() -> McpEndpointRepository:
    return McpEndpointRepository()


def get_mcp_endpoint_service(
    repo: McpEndpointRepository = Depends(get_mcp_endpoint_repository),
) -> McpEndpointService:
    return McpEndpointService(repository=repo)


def require_mcp_endpoint_action(action: ProjectAction) -> Callable[..., dict]:
    def dependency(
        endpoint_id: str,
        current_user: CurrentUser = Depends(get_current_user),
        service: McpEndpointService = Depends(get_mcp_endpoint_service),
        authorization: AuthorizationService = Depends(get_authorization_service),
    ) -> dict:
        endpoint = service.get_endpoint(endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="MCP endpoint not found")
        authorization.authorize(endpoint["project_id"], current_user.user_id, action)
        return endpoint

    return dependency


get_verified_mcp_endpoint = require_mcp_endpoint_action(ProjectAction.ACCESS_READ)
get_writable_mcp_endpoint = require_mcp_endpoint_action(ProjectAction.MCP_MANAGE)
get_credential_mcp_endpoint = require_mcp_endpoint_action(
    ProjectAction.CREDENTIAL_MANAGE
)
