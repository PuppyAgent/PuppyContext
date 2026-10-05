"""Current-reader native operation discovery; never resubmit a mutation here."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from httpx import HTTPError
from postgrest.exceptions import APIError

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction, ProjectGrant
from src.platform.authorization.service import AuthorizationService
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.bootstrap.dependencies import get_product_operation_adapter
from src.version_engine.entrypoints.http.schemas import NativeOperationStatusEnvelope

operations_router = APIRouter()


@operations_router.get(
    "/{project_id}/operations/{request_key}",
    response_model=NativeOperationStatusEnvelope,
    summary="Read the caller's native operation status",
    description="Metadata-only lookup. Pending is not an acknowledgement. Not found does not prove "
                "that remote I/O is absent. Retrying a mutation still requires its exact original input.",
)
async def read_operation_status(
    project_id: str,
    request_key: UUID,
    response: Response,
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    response.headers["Cache-Control"] = "no-store"
    grant = await asyncio.to_thread(authorization.authorize, project_id, current_user.user_id, ProjectAction.CONTENT_READ)
    if not isinstance(grant, ProjectGrant):
        raise HTTPException(status_code=403, detail="Human Project grant required")
    try:
        result = await ops.native_operation_status(project_id, grant, str(request_key))
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
