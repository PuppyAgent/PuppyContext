"""
Workspace API — Folder interface for external Agents

Endpoints:
  POST /workspace/create                Create workspace (returns path)
  POST /workspace/{agent_id}/complete   Trigger merge after Agent completes (via Version Engine)
  GET  /workspace/{agent_id}/status     View workspace status
"""

import os
import time as time_mod

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.billing.runtime import guard_unmetered_hosted_runtime
from src.platform.entitlements.dependencies import get_entitlement_service
from src.platform.entitlements.service import EntitlementService
from src.utils.logger import log_error, log_info
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.bootstrap.dependencies import (
    get_product_operation_adapter,
    get_version_write_command_service,
)

router = APIRouter(
    prefix="/workspace",
    tags=["workspace"],
)


# ============================================================
# Request/Response Models
# ============================================================


class CreateWorkspaceRequest(BaseModel):
    project_id: str
    agent_id: str | None = None


class CreateWorkspaceResponse(BaseModel):
    agent_id: str
    workspace_path: str
    base_commit_id: str | None = None
    mount_command: str


class CompleteWorkspaceResponse(BaseModel):
    agent_id: str
    total_files: int
    committed: int
    conflict_count: int
    strategies: list[str] = []


class WorkspaceStatusResponse(BaseModel):
    agent_id: str
    exists: bool
    workspace_path: str | None = None
    base_commit_id: str | None = None


# ============================================================
# Create Workspace
# ============================================================


@router.post("/create", response_model=ApiResponse[CreateWorkspaceResponse])
async def create_workspace(
    request: CreateWorkspaceRequest,
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
    entitlement_service: EntitlementService = Depends(get_entitlement_service),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    from src.platform.project.write_lease import ProjectWriteLease
    from src.platform.workspace.provider import get_workspace_provider
    from src.platform.workspace.sync_worker import SyncWorker

    grant = authorization.authorize(
        request.project_id, current_user.user_id, ProjectAction.AGENT_RUN
    )
    entitlement_service.require_feature(grant.org_id, "remote_workspace.create")
    guard_unmetered_hosted_runtime("remote_workspace.create")

    agent_id = request.agent_id or f"ext-{int(time_mod.time() * 1000)}"

    provider = get_workspace_provider()
    sync_worker = SyncWorker(
        ops=ops.for_grant(grant),
        base_dir=provider._base_dir,
    )

    # Local materialization is Project-owned host state. Count it in the same
    # deletion admission/drain protocol as durable writers so cleanup cannot
    # race a late workspace recreation.
    async with ProjectWriteLease(request.project_id, "workspace.create"):
        sync_result = await sync_worker.sync_project(request.project_id)
        info = await provider.create_workspace(
            agent_id=agent_id,
            project_id=request.project_id,
            base_commit_id=sync_result.get("head_commit_id") or None,
        )

    mount_cmd = f"docker run -v {info.path}:/workspace your-agent-image"
    log_info(f"[Workspace API] Created workspace: agent={agent_id}, path={info.path}")

    return ApiResponse.success(
        data=CreateWorkspaceResponse(
            agent_id=agent_id,
            workspace_path=info.path,
            base_commit_id=info.base_commit_id,
            mount_command=mount_cmd,
        )
    )


# ============================================================
# Trigger merge after Agent completes (via VersionAdminService)
# ============================================================


@router.post("/{agent_id}/complete", response_model=ApiResponse[CompleteWorkspaceResponse])
async def complete_workspace(
    agent_id: str,
    project_id: str = Query(..., description="Project ID"),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    """
    Called by external Agent after completion - pushes changes via Write Engine

    1. detect_changes: compare workspace vs lower
    2. Build modified/deleted lists
    3. Perform atomic commit via ProductOperationAdapter.bulk_write
    """
    from src.platform.workspace.provider import get_workspace_provider

    authorization.authorize(project_id, current_user.user_id, ProjectAction.CONTENT_WRITE)

    provider = get_workspace_provider()

    # Verify the agent's workspace actually belongs to the caller-supplied
    # project_id — otherwise a caller with access to project A could merge
    # another project B's workspace into A by supplying a mismatched agent_id.
    ws_info = provider.get_workspace_info(agent_id)
    if ws_info is not None and ws_info.project_id != project_id:
        raise HTTPException(
            status_code=403,
            detail="Workspace does not belong to this project",
        )

    changes = await provider.detect_changes(agent_id)

    modified: dict[str, bytes] = {}
    for rel_path, content in changes.modified.items():
        if isinstance(content, str):
            modified[rel_path] = content.encode("utf-8")
        elif isinstance(content, bytes):
            modified[rel_path] = content
        else:
            modified[rel_path] = str(content).encode("utf-8")

    deleted = list(changes.deleted)
    total_files = len(changes.modified) + len(changes.deleted)

    try:
        if not changes.modified and not changes.deleted:
            return ApiResponse.success(
                data=CompleteWorkspaceResponse(
                    agent_id=agent_id,
                    total_files=0,
                    committed=0,
                    conflict_count=0,
                ),
                message="No changes detected",
            )

        outcome = await commands.bulk_write(
            project_id,
            modified,
            actor=agent_id,
            deleted=deleted,
            message=f"Agent workspace merge ({len(modified)} modified, {len(deleted)} deleted)",
        )
        result = outcome.result
        committed = len(modified)
        conflict_count = result.conflicts
        strategies = ["merge"] if result.merged else []
        log_info(
            f"[Workspace API] version push: commit={result.commit_id or '(none)'} "
            f"merged={result.merged} files={committed}"
        )
    except Exception as e:
        log_error(f"[Workspace API] version push failed: {e}")
        raise HTTPException(status_code=500, detail="Workspace merge failed") from e
    finally:
        await provider.cleanup(agent_id)

    log_info(
        f"[Workspace API] Completed: agent={agent_id}, "
        f"committed={committed}, conflicts={conflict_count}"
    )

    return ApiResponse.success(
        data=CompleteWorkspaceResponse(
            agent_id=agent_id,
            total_files=total_files,
            committed=committed,
            conflict_count=conflict_count,
            strategies=strategies,
        )
    )


# ============================================================
# View Workspace Status
# ============================================================


@router.get("/{agent_id}/status", response_model=ApiResponse[WorkspaceStatusResponse])
async def workspace_status(
    agent_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    from src.platform.workspace.provider import get_workspace_provider

    provider = get_workspace_provider()
    info = provider.get_workspace_info(agent_id)

    # A workspace is bound to a project — only members of that project may
    # observe its status/path. Without this any authenticated user could probe
    # arbitrary agent_ids and learn another tenant's workspace paths.
    if info is not None:
        authorization.authorize(info.project_id, current_user.user_id, ProjectAction.AGENT_READ)

    if info and os.path.exists(info.path):
        return ApiResponse.success(
            data=WorkspaceStatusResponse(
                agent_id=agent_id,
                exists=True,
                workspace_path=info.path,
                base_commit_id=info.base_commit_id,
            )
        )

    return ApiResponse.success(
        data=WorkspaceStatusResponse(
            agent_id=agent_id,
            exists=False,
        )
    )
