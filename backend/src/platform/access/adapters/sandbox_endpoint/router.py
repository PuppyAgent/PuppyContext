import asyncio
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from src.common_schemas import ApiResponse
from src.platform.access.adapters.agent.sandbox_session import SandboxFile
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
from src.platform.repository_target.models import RepositoryPathProjection
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


def _clone_version_files(project_id: str, scope_path: str) -> tuple:
    """Clone version tree files for a scope path. Returns (client, files_dict)."""
    from src.version_engine.adapters.batch.in_process_client import InProcessVersionClient
    from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container

    repo_manager = build_worker_version_engine_container().repo_manager
    client = InProcessVersionClient(
        repo_manager,
        project_id,
        RepositoryPathProjection(path_prefix=scope_path),
        actor="sandbox_endpoint",
    )
    files = client.clone()
    return client, files


def _build_sandbox_files_from_clone(
    cloned_files: dict[str, bytes],
    mount_path: str,
    scope_path: str,
) -> list[SandboxFile]:
    """Convert version clone result to SandboxFile list for container mounting."""
    sandbox_files: list[SandboxFile] = []
    for file_path, content in cloned_files.items():
        relative = file_path
        if scope_path and relative.startswith(scope_path + "/"):
            relative = relative[len(scope_path) + 1 :]

        target = f"{mount_path}/{relative}"

        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        if ext == "json":
            try:
                text = content.decode("utf-8")
                json.loads(text)
                content_type = "application/json"
            except Exception:
                text = content.decode("utf-8", errors="replace")
                content_type = "text/plain"
        elif ext in ("md", "markdown"):
            text = content.decode("utf-8", errors="replace")
            content_type = "text/markdown"
        else:
            text = content.decode("utf-8", errors="replace")
            content_type = "text/plain"

        sandbox_files.append(
            SandboxFile(
                path=target,
                content=text,
                content_type=content_type,
                version_path=file_path,
                node_type="json"
                if ext == "json"
                else "markdown"
                if ext in ("md", "markdown")
                else "file",
            )
        )
    return sandbox_files


async def _read_modified_files(
    sandbox_service: SandboxService,
    session_id: str,
    original_files: dict[str, bytes],
    mount_path: str,
    scope_path: str,
) -> dict[str, bytes]:
    """Read files from sandbox container, return only changed ones as {version_path: bytes}."""
    from src.platform.access.adapters.agent.sandbox_session import _read_modified_files as _read_mod

    return await _read_mod(sandbox_service, session_id, original_files, mount_path, scope_path)


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

    command = (payload or {}).get("command", "")
    if not command or not isinstance(command, str):
        raise HTTPException(status_code=400, detail="command is required")

    mounts = endpoint.get("mounts", [])
    _validate_command(command, mounts)

    project_id = endpoint.get("project_id", "")
    if not project_id:
        raise HTTPException(status_code=400, detail="Sandbox endpoint has no project_id")

    # Clone version tree for each mount, collect files + clients
    all_sandbox_files: list[SandboxFile] = []
    version_clients = []
    for mount in mounts:
        mount_source = mount.get("path")
        if not mount_source:
            continue
        mount_target = _normalize_mount_path(mount.get("mount_path", "/workspace"))
        permissions = mount.get("permissions") or {}
        has_write = permissions.get("write", False)

        client, cloned_files = await asyncio.to_thread(
            _clone_version_files,
            project_id,
            mount_source,
        )

        sandbox_files = _build_sandbox_files_from_clone(cloned_files, mount_target, mount_source)
        all_sandbox_files.extend(sandbox_files)

        if has_write:
            version_clients.append((client, cloned_files, mount_target, mount_source))

    if not all_sandbox_files:
        raise HTTPException(
            status_code=400, detail="No mount files resolved for this sandbox endpoint"
        )

    session_id = f"sbxep-{endpoint_id}-{uuid.uuid4().hex[:10]}"
    start_res = await sandbox_service.start_with_files(
        session_id=session_id,
        files=all_sandbox_files,
        readonly=not version_clients,
        s3_service=None,
        audit_context={
            "source": "sandbox_endpoint",
            "session_id": session_id,
            "org_id": endpoint.get("org_id"),
            "project_id": project_id,
            "maximum_runtime_units": max(
                1,
                int(endpoint.get("timeout_seconds") or 30) // 60 + 1,
            ),
        },
    )
    if not start_res.get("success"):
        raise HTTPException(
            status_code=500, detail=start_res.get("error", "Failed to start sandbox session")
        )

    try:
        exec_res = await sandbox_service.exec(
            session_id=session_id,
            command=command,
            audit_context={
                "source": "sandbox_endpoint",
                "session_id": session_id,
                "org_id": endpoint.get("org_id"),
                "project_id": project_id,
                "maximum_runtime_units": max(
                    1,
                    int(endpoint.get("timeout_seconds") or 30) // 60 + 1,
                ),
            },
        )

        # Write back changed files for writable mounts via Write Engine
        writeback_results = []
        for client, original_files, mount_target, scope_path in version_clients:
            modified = await _read_modified_files(
                sandbox_service,
                session_id,
                original_files,
                mount_target,
                scope_path,
            )
            if modified:
                from src.version_engine.derived.hooks import push_and_finalize

                push_result = await push_and_finalize(
                    client,
                    project_id,
                    modified=modified,
                    message=f"Sandbox exec: {command[:80]}",
                )
                writeback_results.append(
                    {
                        "scope": scope_path,
                        "files_written": len(modified),
                        "commit_id": push_result.get("commit_id"),
                    }
                )

        if writeback_results:
            exec_res["writeback"] = writeback_results

        return ApiResponse.success(data=exec_res)
    finally:
        await sandbox_service.stop(session_id=session_id)
