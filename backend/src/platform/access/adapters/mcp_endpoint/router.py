from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query

from src.platform.access.adapters.mcp_endpoint.service import McpEndpointService
from src.platform.access.adapters.mcp_endpoint.schemas import (
    McpEndpointCreate,
    McpEndpointUpdate,
    McpEndpointOut,
)
from src.platform.access.adapters.mcp_endpoint.dependencies import (
    get_mcp_endpoint_service,
    get_verified_mcp_endpoint,
    get_writable_mcp_endpoint,
    get_credential_mcp_endpoint,
)
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.common_schemas import ApiResponse
from src.config import settings
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService


def _mcp_server_url() -> str:
    """Public MCP proxy URL external clients connect to (auth via Authorization: Bearer)."""
    base = (settings.PUBLIC_URL or "").rstrip("/")
    return f"{base}/api/v1/mcp/proxy" if base else ""


router = APIRouter(
    prefix="/mcp-endpoints",
    tags=["mcp-endpoints"],
    responses={
        404: {"description": "MCP endpoint not found"},
        403: {"description": "Access denied"},
    },
)


def _to_out(row: dict) -> McpEndpointOut:
    return McpEndpointOut(
        id=row["id"],
        project_id=row["project_id"],
        path=row.get("path"),
        name=row["name"],
        description=row.get("description"),
        api_key=row.get("api_key", ""),
        api_key_hint=row.get("api_key_hint", ""),
        api_key_revealed=bool(row.get("api_key_revealed", False)),
        server_url=_mcp_server_url(),
        tools_config=row.get("tools_config", {}),
        accesses=row.get("accesses", []),
        created_by=row.get("created_by"),
        config=row.get("config", {}),
        status=row["status"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


@router.get(
    "",
    response_model=ApiResponse[List[McpEndpointOut]],
    summary="List MCP endpoints for a project",
)
def list_endpoints(
    project_id: str = Query(..., description="Project ID"),
    current_user: CurrentUser = Depends(get_current_user),
    service: McpEndpointService = Depends(get_mcp_endpoint_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    authorization.authorize(
        project_id, current_user.user_id, ProjectAction.ACCESS_READ
    )
    rows = service.list_endpoints(project_id)
    return ApiResponse.success(data=[_to_out(r) for r in rows])


@router.get(
    "/{endpoint_id}",
    response_model=ApiResponse[McpEndpointOut],
    summary="Get MCP endpoint details",
)
def get_endpoint(
    endpoint: dict = Depends(get_verified_mcp_endpoint),
):
    return ApiResponse.success(data=_to_out(endpoint))


@router.get(
    "/by-path/{path:path}",
    response_model=ApiResponse[McpEndpointOut],
    summary="Get MCP endpoint by path",
)
def get_by_path(
    path: str,
    current_user: CurrentUser = Depends(get_current_user),
    service: McpEndpointService = Depends(get_mcp_endpoint_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    row = service.get_by_path(path)
    if not row:
        raise HTTPException(status_code=404, detail="No MCP endpoint for this path")
    authorization.authorize(
        row["project_id"], current_user.user_id, ProjectAction.ACCESS_READ
    )
    return ApiResponse.success(data=_to_out(row))


@router.post(
    "",
    response_model=ApiResponse[McpEndpointOut],
    summary="Create MCP endpoint",
)
def create_endpoint(
    payload: McpEndpointCreate,
    current_user: CurrentUser = Depends(get_current_user),
    service: McpEndpointService = Depends(get_mcp_endpoint_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    authorization.authorize(
        payload.project_id, current_user.user_id, ProjectAction.MCP_MANAGE
    )
    row = service.create_endpoint(
        project_id=payload.project_id,
        name=payload.name,
        path=payload.path,
        description=payload.description,
        accesses=payload.accesses,
        tools_config=payload.tools_config,
        created_by=current_user.user_id,
    )
    return ApiResponse.success(data=_to_out(row), message="MCP endpoint created")


@router.put(
    "/{endpoint_id}",
    response_model=ApiResponse[McpEndpointOut],
    summary="Update MCP endpoint",
)
def update_endpoint(
    payload: McpEndpointUpdate,
    endpoint: dict = Depends(get_writable_mcp_endpoint),
    current_user: CurrentUser = Depends(get_current_user),
    service: McpEndpointService = Depends(get_mcp_endpoint_service),
):
    update_kwargs = payload.model_dump(exclude_unset=True)
    row = service.update_endpoint(endpoint["id"], **update_kwargs)
    if not row:
        raise HTTPException(status_code=500, detail="Update failed")
    return ApiResponse.success(data=_to_out(row))


@router.delete(
    "/{endpoint_id}",
    response_model=ApiResponse,
    summary="Delete MCP endpoint",
)
def delete_endpoint(
    endpoint: dict = Depends(get_writable_mcp_endpoint),
    current_user: CurrentUser = Depends(get_current_user),
    service: McpEndpointService = Depends(get_mcp_endpoint_service),
):
    service.delete_endpoint(endpoint["id"])
    return ApiResponse.success(message="MCP endpoint deleted")


@router.post(
    "/{endpoint_id}/regenerate-key",
    response_model=ApiResponse[McpEndpointOut],
    summary="Regenerate API key",
)
def regenerate_key(
    endpoint: dict = Depends(get_credential_mcp_endpoint),
    current_user: CurrentUser = Depends(get_current_user),
    service: McpEndpointService = Depends(get_mcp_endpoint_service),
):
    row = service.regenerate_key(endpoint["id"])
    if not row:
        raise HTTPException(status_code=500, detail="Regenerate failed")
    return ApiResponse.success(data=_to_out(row), message="API key regenerated")
