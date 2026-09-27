from typing import Callable

from fastapi import Depends, HTTPException

from src.platform.access.adapters.sandbox_endpoint.repository import SandboxEndpointRepository
from src.platform.access.adapters.sandbox_endpoint.service import SandboxEndpointService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService


def get_sandbox_endpoint_repository() -> SandboxEndpointRepository:
    return SandboxEndpointRepository()


def get_sandbox_endpoint_service(
    repo: SandboxEndpointRepository = Depends(get_sandbox_endpoint_repository),
) -> SandboxEndpointService:
    return SandboxEndpointService(repository=repo)


def require_sandbox_endpoint_action(action: ProjectAction) -> Callable[..., dict]:
    def dependency(
        endpoint_id: str,
        current_user: CurrentUser = Depends(get_current_user),
        service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
        authorization: AuthorizationService = Depends(get_authorization_service),
    ) -> dict:
        endpoint = service.get_endpoint(endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="Sandbox endpoint not found")
        authorization.authorize(endpoint["project_id"], current_user.user_id, action)
        return endpoint

    return dependency


get_verified_sandbox_endpoint = require_sandbox_endpoint_action(
    ProjectAction.ACCESS_READ
)
get_writable_sandbox_endpoint = require_sandbox_endpoint_action(
    ProjectAction.SANDBOX_MANAGE
)
get_credential_sandbox_endpoint = require_sandbox_endpoint_action(
    ProjectAction.CREDENTIAL_MANAGE
)
