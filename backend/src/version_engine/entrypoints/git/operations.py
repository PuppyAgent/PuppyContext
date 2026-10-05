"""Known native operation keys under exact Project-root Runtime credentials.

This does not add a request-key extension to the stock Git wire protocol. A
caller must already know its key; a Human JWT is not a Runtime credential.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from httpx import HTTPError
from postgrest.exceptions import APIError

from src.platform.authorization.models import RuntimeGrant
from src.version_engine.bootstrap.dependencies import get_repo_manager
from src.version_engine.entrypoints.git.auth import resolve_git_project_auth
from src.version_engine.entrypoints.http.schemas import NativeOperationStatusEnvelope
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager

operations_router = APIRouter()


@operations_router.get(
    "/{project_id}.git/operations/{request_key}",
    response_model=NativeOperationStatusEnvelope,
    summary="Read a known native operation under its original Runtime principal",
)
async def read_git_operation_status(
    project_id: str,
    request_key: UUID,
    request: Request,
    response: Response,
    manager: VersionRepoManager = Depends(get_repo_manager),
):
    response.headers["Cache-Control"] = "no-store"
    auth = await resolve_git_project_auth(project_id, request)
    grant = auth.get("_runtime_grant")
    if not isinstance(grant, RuntimeGrant):
        raise HTTPException(status_code=403, detail="Runtime grant required")
    try:
        result = await asyncio.to_thread(manager.get_native_operation_status, project_id, grant, str(request_key))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Repository action denied") from exc
    except APIError as exc:
        if str(exc.code) == "42501":
            raise HTTPException(status_code=403, detail="Repository action denied") from exc
        raise HTTPException(status_code=503, detail="Repository operation lookup is temporarily unavailable") from exc
    except (RuntimeError, HTTPError) as exc:
        raise HTTPException(status_code=503, detail="Repository operation lookup is temporarily unavailable") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Native operation not found", headers={"Cache-Control": "no-store"})
    return NativeOperationStatusEnvelope.success(data=result)
