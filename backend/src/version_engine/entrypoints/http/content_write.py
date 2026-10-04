"""Content Write API — write, mkdir, mv, rm, bulk-write."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from src.common_schemas import ApiResponse
from src.version_engine.bootstrap.dependencies import get_version_write_command_service
from src.version_engine.entrypoints.http.schemas import (
    BulkWriteRequest,
    MkdirRequest,
    MoveRequest,
    RemoveRequest,
    WriteFileRequest,
)
from src.version_engine.write_engine.engine import ConcurrentMutationError
from src.version_engine.admission.permission import require_project_write_state
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.write_engine.trace import VersionTrace, use_version_trace
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser

write_router = APIRouter()


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
):
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
):
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
):
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
):
    """Delete one or more paths from the current version tree."""
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
):
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
