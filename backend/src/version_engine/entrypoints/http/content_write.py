"""Content Write API — write, mkdir, mv, rm, bulk-write."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException
from postgrest.exceptions import APIError

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.version_engine.adapters.product.commands import VersionWriteCommandService
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

write_router = APIRouter()


def _native_rpc_error(exc):
    if str(exc.code) == "42501":
        return HTTPException(status_code=403, detail="Repository action denied")
    if exc.message in {"request_key_reused", "generation_mismatch"}:
        return HTTPException(
            status_code=409, detail="Native operation identity or starting revision changed"
        )
    if exc.message == "file_size_limit_exceeded":
        return HTTPException(status_code=413, detail="File size limit exceeded")
    if (
        exc.message.startswith("storage_quota_exceeded:")
        or exc.message == "repository_capacity_exceeded"
    ):
        return HTTPException(status_code=413, detail="Repository storage limit exceeded")
    return HTTPException(status_code=503, detail="Repository write is temporarily unavailable")


async def _native_write(project_id, body, commands, current_user, authorization, operation):
    # Replay needs current read authority, not a new write entitlement. New
    # publication subsequently checks CONTENT_WRITE and the live SQL actor.
    grant = await asyncio.to_thread(
        authorization.authorize, project_id, current_user.user_id, ProjectAction.CONTENT_READ
    )
    try:
        import uuid

        native = body.native
        if native is None:

            def capture():
                with commands.ops.open_read(project_id, grant) as reader:
                    return reader.get_read_revision(project_id)

            base = await asyncio.to_thread(capture)
            request_key, byte_paths = str(uuid.uuid4()), None
        else:
            base, request_key, byte_paths = (
                native.repository_revision,
                native.request_key,
                native.byte_paths,
            )
        data = await commands.native_operation(
            project_id,
            grant,
            operation=operation,
            arguments=body.model_dump(exclude={"native"}, exclude_unset=True),
            request_key=request_key,
            base=base,
            byte_paths=byte_paths,
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
        raise HTTPException(
            status_code=503, detail="Repository write is temporarily unavailable"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Repository write is temporarily unavailable"
        ) from exc


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
    return await _native_write(project_id, body, commands, current_user, authorization, "write")


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
    return await _native_write(project_id, body, commands, current_user, authorization, "mkdir")


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
    return await _native_write(project_id, body, commands, current_user, authorization, "move")


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
    return await _native_write(project_id, body, commands, current_user, authorization, "remove")


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
    return await _native_write(
        project_id, body, commands, current_user, authorization, "bulk_write"
    )
