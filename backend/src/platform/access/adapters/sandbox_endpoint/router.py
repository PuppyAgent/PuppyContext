import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from src.common_schemas import ApiResponse
from src.platform.access.adapters.sandbox_endpoint.dependencies import (
    get_credential_sandbox_endpoint,
    get_sandbox_endpoint_service,
    get_verified_sandbox_endpoint,
    get_writable_sandbox_endpoint,
)
from src.platform.access.adapters.sandbox_endpoint.schemas import (
    SandboxEndpointCreate,
    SandboxEndpointOut,
    SandboxEndpointUpdate,
)
from src.platform.access.adapters.sandbox_endpoint.service import SandboxEndpointService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.scope_sandbox.execution.dependencies import get_sandbox_service
from src.platform.scope_sandbox.execution.service import SandboxService

router = APIRouter(
    prefix="/sandbox-endpoints",
    tags=["sandbox-endpoints"],
    responses={
        404: {"description": "Sandbox endpoint not found"},
        403: {"description": "Access denied"},
    },
)
logger = logging.getLogger(__name__)


def _to_out(row: dict) -> SandboxEndpointOut:
    return SandboxEndpointOut(
        id=row["id"],
        project_id=row["project_id"],
        path=row.get("path"),
        name=row["name"],
        description=row.get("description"),
        access_key=row["access_key"],
        has_key=row.get("has_key", False),
        key_last4=row.get("key_last4"),
        mounts=row.get("mounts", []),
        runtime=row.get("runtime", "alpine"),
        timeout_seconds=row.get("timeout_seconds", 30),
        resource_limits=row.get("resource_limits", {}),
        status=row["status"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _normalize_mount_path(path: str) -> str:
    normalized = (path or "/workspace").strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    if not normalized.startswith("/workspace"):
        normalized = f"/workspace{normalized}" if normalized != "/" else "/workspace"
    return normalized or "/workspace"


def _is_write_command(command: str) -> bool:
    write_patterns = [
        r">",
        r">>",
        r"\brm\b",
        r"\bmv\b",
        r"\bcp\b",
        r"\btouch\b",
        r"\bmkdir\b",
        r"\brmdir\b",
        r"\btruncate\b",
        r"\bsed\s+-i\b",
        r"\btee\b",
        r"\bchmod\b",
        r"\bchown\b",
        r"\becho\b.*>",
    ]
    return any(re.search(pattern, command) for pattern in write_patterns)


def _validate_command(command: str, mounts: list[dict[str, Any]]) -> None:
    # Forbidden-pattern policy lives in the shared choke point so the endpoint
    # and the agent bash tool stay in lockstep (ISSUE-009).
    from src.platform.scope_sandbox.execution_policy import (
        SandboxCommandRejected,
        assert_command_allowed,
    )

    try:
        assert_command_allowed(command)
    except SandboxCommandRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    readonly_mounts = [
        _normalize_mount_path(m.get("mount_path", "/workspace"))
        for m in mounts
        if (m.get("permissions") or {}).get("write") is False
    ]
    if not readonly_mounts:
        return

    if not _is_write_command(command):
        return

    referenced_paths = re.findall(r"(/workspace[^\s\"']*)", command)
    if not referenced_paths:
        raise HTTPException(
            status_code=400, detail="Write commands must target explicit /workspace paths"
        )

    for path in referenced_paths:
        for readonly_path in readonly_mounts:
            if path == readonly_path or path.startswith(f"{readonly_path}/"):
                raise HTTPException(
                    status_code=403, detail=f"Write denied for readonly mount: {readonly_path}"
                )


@router.get(
    "",
    response_model=ApiResponse[list[SandboxEndpointOut]],
    summary="List Sandbox endpoints for a project",
)
def list_endpoints(
    project_id: str = Query(..., description="Project ID"),
    current_user: CurrentUser = Depends(get_current_user),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    authorization.authorize(project_id, current_user.user_id, ProjectAction.ACCESS_READ)
    rows = service.list_endpoints(project_id)
    return ApiResponse.success(data=[_to_out(r) for r in rows])


@router.get(
    "/{endpoint_id}",
    response_model=ApiResponse[SandboxEndpointOut],
    summary="Get Sandbox endpoint details",
)
def get_endpoint(
    endpoint: dict = Depends(get_verified_sandbox_endpoint),
):
    return ApiResponse.success(data=_to_out(endpoint))


@router.get(
    "/by-path/{path:path}",
    response_model=ApiResponse[SandboxEndpointOut],
    summary="Get Sandbox endpoint by path",
)
def get_by_path(
    path: str,
    current_user: CurrentUser = Depends(get_current_user),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    row = service.get_by_path(path)
    if not row:
        raise HTTPException(status_code=404, detail="No Sandbox endpoint for this path")
    authorization.authorize(row["project_id"], current_user.user_id, ProjectAction.ACCESS_READ)
    return ApiResponse.success(data=_to_out(row))


@router.post(
    "",
    response_model=ApiResponse[SandboxEndpointOut],
    summary="Create Sandbox endpoint",
)
def create_endpoint(
    payload: SandboxEndpointCreate,
    current_user: CurrentUser = Depends(get_current_user),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    authorization.authorize(payload.project_id, current_user.user_id, ProjectAction.SANDBOX_MANAGE)
    row = service.create_endpoint(
        project_id=payload.project_id,
        name=payload.name,
        path=payload.path,
        description=payload.description,
        mounts=payload.mounts,
        runtime=payload.runtime,
        timeout_seconds=payload.timeout_seconds,
        resource_limits=payload.resource_limits,
    )
    return ApiResponse.success(data=_to_out(row), message="Sandbox endpoint created")


@router.put(
    "/{endpoint_id}",
    response_model=ApiResponse[SandboxEndpointOut],
    summary="Update Sandbox endpoint",
)
def update_endpoint(
    payload: SandboxEndpointUpdate,
    endpoint: dict = Depends(get_writable_sandbox_endpoint),
    current_user: CurrentUser = Depends(get_current_user),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
):
    update_kwargs = payload.model_dump(exclude_unset=True)
    row = service.update_endpoint(endpoint["id"], **update_kwargs)
    if not row:
        raise HTTPException(status_code=500, detail="Update failed")
    return ApiResponse.success(data=_to_out(row))


@router.delete(
    "/{endpoint_id}",
    response_model=ApiResponse,
    summary="Delete Sandbox endpoint",
)
def delete_endpoint(
    endpoint: dict = Depends(get_writable_sandbox_endpoint),
    current_user: CurrentUser = Depends(get_current_user),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
):
    service.delete_endpoint(endpoint["id"])
    return ApiResponse.success(message="Sandbox endpoint deleted")


@router.post(
    "/{endpoint_id}/regenerate-key",
    response_model=ApiResponse[SandboxEndpointOut],
    summary="Regenerate access key",
)
def regenerate_key(
    endpoint: dict = Depends(get_credential_sandbox_endpoint),
    current_user: CurrentUser = Depends(get_current_user),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
):
    row = service.regenerate_key(endpoint["id"])
    if not row:
        raise HTTPException(status_code=500, detail="Regenerate failed")
    return ApiResponse.success(data=_to_out(row), message="Access key regenerated")


@router.post(
    "/{endpoint_id}/exec",
    response_model=ApiResponse[dict],
    summary="Execute command in Sandbox endpoint",
)
async def exec_command(
    endpoint_id: str,
    payload: dict,
    x_access_key: str = Header(..., alias="X-Access-Key"),
    service: SandboxEndpointService = Depends(get_sandbox_endpoint_service),
    sandbox_service: SandboxService = Depends(get_sandbox_service),
):
    try:
        endpoint = service.get_by_access_key(x_access_key)
    except Exception:
        # Credential validation is the security boundary for this public route.
        # A backing-store outage must fail closed with a retriable service error,
        # rather than leaking an internal exception as HTTP 500 or proceeding
        # toward sandbox execution.
        logger.exception("sandbox_endpoint_credential_validation_unavailable")
        raise HTTPException(
            status_code=503,
            detail="Sandbox credential validation is temporarily unavailable",
        ) from None
    if not endpoint:
        raise HTTPException(status_code=403, detail="Invalid access key")
    if endpoint.get("id") != endpoint_id:
        raise HTTPException(status_code=403, detail="Invalid access key")
    if endpoint.get("status") != "active":
        raise HTTPException(status_code=403, detail="Sandbox endpoint is not active")

    raise HTTPException(
        status_code=501,
        detail={
            "code": "native_scope_not_available",
            "message": "Scoped sandbox endpoints require the new repository Scope implementation",
        },
    )
