"""Content Write API — write, mkdir, mv, rm, bulk-write."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException
from postgrest.exceptions import APIError

from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService

from src.common_schemas import ApiResponse
from src.version_engine.bootstrap.dependencies import get_version_write_command_service
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.entrypoints.http.content_helpers import display_git_path
from src.version_engine.entrypoints.http.schemas import (
    BulkWriteRequest,
    MkdirRequest,
    MoveRequest,
    RemoveRequest,
    WriteFileRequest,
)
from src.version_engine.write_engine.engine import ConcurrentMutationError
from src.version_engine.write_engine.errors import NativeRevisionConflictError
from src.version_engine.admission.permission import require_project_write_state
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.write_engine.trace import VersionTrace, use_version_trace
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser

write_router = APIRouter()


def _native_rpc_error(exc):
    if str(exc.code) == "42501":
        return HTTPException(status_code=403, detail="Repository action denied")
    if exc.message in {"request_key_reused", "generation_mismatch"}:
        return HTTPException(status_code=409, detail="Native operation identity or starting revision changed")
    if exc.message == "file_size_limit_exceeded":
        return HTTPException(status_code=413, detail="File size limit exceeded")
    if exc.message.startswith("storage_quota_exceeded:") or exc.message == "repository_capacity_exceeded":
        return HTTPException(status_code=413, detail="Repository storage limit exceeded")
    return HTTPException(status_code=503, detail="Repository write is temporarily unavailable")


async def _native_write(project_id, body, commands, current_user, authorization, operation):
    if body.native is None:
        return None
    # Replay needs current read authority, not a new write entitlement. New
    # publication subsequently checks CONTENT_WRITE and the live SQL actor.
    grant = await asyncio.to_thread(authorization.authorize, project_id, current_user.user_id, ProjectAction.CONTENT_READ)
    try:
        data = await commands.native_operation(
            project_id, grant, operation=operation, arguments=body.model_dump(exclude={"native"}, exclude_unset=True),
            request_key=body.native.request_key, base=body.native.repository_revision,
            byte_paths=body.native.byte_paths,
        )
        return ApiResponse.success(data=data)
    except (ConcurrentMutationError, NativeRevisionConflictError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=display_git_path(str(exc))) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=display_git_path(str(exc))) from exc
    except (ValueError, NotADirectoryError, IsADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=display_git_path(str(exc))) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Repository action denied") from exc
    except APIError as exc:
        raise _native_rpc_error(exc) from exc
    except StorageWriteError as exc:
        cause = exc.__cause__
        for _ in range(8):
            if isinstance(cause, APIError):
                raise _native_rpc_error(cause) from exc
            if cause is None:
                break
            cause = cause.__cause__
        raise HTTPException(status_code=503, detail="Repository write is temporarily unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Repository write is temporarily unavailable") from exc


@write_router.post(
    "/{project_id}/write",
    summary="Write a file",
)
async def write_file_endpoint(
    project_id: str,
    body: WriteFileRequest,
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
    current_user: CurrentUser = Depends(get_current_user),
    x_puppyone_client_id: str | None = Header(default=None, alias="X-PuppyOne-Client-Id"),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    native = await _native_write(project_id, body, commands, current_user, authorization, "write")
    if native is not None:
        return native
    trace = VersionTrace(
        "content.write",
        project_id=project_id,
        actor=f"user:{current_user.user_id}",
        source_channel="papi",
    )
    with use_version_trace(trace):
        try:
            with trace.phase("db.get_project_write_state"):
                write_state = require_project_write_state(
                    commands.ops,
                    project_id,
                    current_user.user_id,
                )

            who = f"user:{current_user.user_id}"
            with trace.phase(
                "commands.write_file",
                path=body.path,
                base_commit_id=body.base_commit_id or "",
            ):
                outcome = await commands.write_file(
                    project_id,
                    body.path,
                    body.content,
                    node_type=body.node_type,
                    actor=who,
                    message=body.message,
                    default_message_prefix="edit",
                    base_commit_id=body.base_commit_id,
                    project_write_state=write_state,
                    pusher_client_id=x_puppyone_client_id or "",
                )
                result = outcome.result
                trace.mark(
                    "command.normalized",
                    path=outcome.path,
                    size_bytes=outcome.size_bytes,
                )
        except ConcurrentMutationError as e:
            trace.finish(status="conflict", path=getattr(body, "path", ""))
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception:
            trace.finish(status="error", path=getattr(body, "path", ""))
            raise

        trace.finish(
            status="ok",
            commit_id=result.commit_id,
            path=outcome.path,
            merged=result.merged,
            conflicts=result.conflicts,
        )
        return ApiResponse.success(data={
            "commit_id": result.commit_id,
            "path": outcome.path,
            "merged": result.merged,
            "conflicts": result.conflicts,
        })


@write_router.post(
    "/{project_id}/mkdir",
    summary="Create a directory",
)
async def mkdir(
    project_id: str,
    body: MkdirRequest,
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    native = await _native_write(project_id, body, commands, current_user, authorization, "mkdir")
    if native is not None:
        return native
    write_state = require_project_write_state(commands.ops, project_id, current_user.user_id)
    who = f"user:{current_user.user_id}"
    try:
        outcome = await commands.mkdir(
            project_id,
            body.path,
            actor=who,
            base_commit_id=body.base_commit_id,
            project_write_state=write_state,
        )
        result = outcome.result
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ApiResponse.success(data={"path": outcome.path, "commit_id": result.commit_id})


@write_router.post(
    "/{project_id}/mv",
    summary="Move/rename",
)
async def move(
    project_id: str,
    body: MoveRequest,
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    native = await _native_write(project_id, body, commands, current_user, authorization, "move")
    if native is not None:
        return native
    write_state = require_project_write_state(commands.ops, project_id, current_user.user_id)
    old_clean = commands.normalize_path(body.old_path)
    new_clean = commands.normalize_path(body.new_path)
    who = f"user:{current_user.user_id}"

    # Honor no_clobber: the underlying move always overwrites, but MoveRequest
    # advertises this POSIX-mv flag, so enforce "refuse to overwrite an existing
    # destination" here rather than silently ignoring the field.
    if body.no_clobber and commands.ops.stat(project_id, new_clean) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Destination already exists: {new_clean} (no_clobber)",
        )

    try:
        outcome = await commands.move(
            project_id,
            old_clean,
            new_clean,
            actor=who,
            message=body.message,
            default_message_prefix="moved",
            base_commit_id=body.base_commit_id,
            project_write_state=write_state,
        )
        result = outcome.result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return ApiResponse.success(data={
        "commit_id": result.commit_id,
        "old_path": outcome.old_path,
        "new_path": outcome.new_path,
    })


@write_router.post(
    "/{project_id}/rm",
    summary="Delete files or directories",
)
async def remove(
    project_id: str,
    body: RemoveRequest,
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    """Delete one or more paths from the current version tree."""
    native = await _native_write(project_id, body, commands, current_user, authorization, "remove")
    if native is not None:
        return native
    write_state = require_project_write_state(commands.ops, project_id, current_user.user_id)
    who = f"user:{current_user.user_id}"

    paths = body.paths
    if paths:
        clean = commands.normalize_paths(paths)
        if not clean:
            raise HTTPException(status_code=400, detail="paths is empty")
        if not body.force:
            missing = [p for p in clean if commands.ops.stat(project_id, p) is None]
            if missing:
                raise HTTPException(status_code=404, detail=f"Path not found: {missing[0]}")
        try:
            outcome = await commands.delete(
                project_id,
                clean,
                actor=who,
                base_commit_id=body.base_commit_id,
                project_write_state=write_state,
            )
            result = outcome.result
        except ConcurrentMutationError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return ApiResponse.success(data={
            "commit_id": result.commit_id,
            "paths": outcome.paths or clean,
        })

    clean_path = commands.normalize_path(body.path)
    if not body.force and commands.ops.stat(project_id, clean_path) is None:
        raise HTTPException(status_code=404, detail=f"Path not found: {clean_path}")
    try:
        outcome = await commands.delete(
            project_id,
            [clean_path],
            actor=who,
            base_commit_id=body.base_commit_id,
            project_write_state=write_state,
        )
        result = outcome.result
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ApiResponse.success(data={
        "commit_id": result.commit_id,
        "path": (outcome.paths or [clean_path])[0],
    })


@write_router.post(
    "/{project_id}/bulk-write",
    summary="Bulk write files",
)
async def bulk_write(
    project_id: str,
    body: BulkWriteRequest,
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    native = await _native_write(project_id, body, commands, current_user, authorization, "bulk_write")
    if native is not None:
        return native
    write_state = require_project_write_state(commands.ops, project_id, current_user.user_id)
    files = {item.path: item.content for item in body.files}
    node_types = {item.path: item.node_type for item in body.files}

    who = f"user:{current_user.user_id}"
    try:
        outcome = await commands.bulk_write(
            project_id,
            files,
            actor=who,
            node_types=node_types,
            message=body.message,
            default_message="bulk write",
            base_commit_id=body.base_commit_id,
            project_write_state=write_state,
        )
        result = outcome.result
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return ApiResponse.success(data={
        "commit_id": result.commit_id,
        "total": len(outcome.paths or []),
        "merged": result.merged,
    })
