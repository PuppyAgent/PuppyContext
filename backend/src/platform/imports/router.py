"""Routes for durable one-time imports."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.imports.dependencies import get_import_job_service
from src.platform.imports.schemas import (
    ImportJobCreateRequest,
    ImportJobListResponse,
    ImportJobResponse,
)
from src.platform.imports.service import ImportJobService
from src.platform.imports.providers import get_import_provider_registry, import_specs
from src.provider.registry import ProviderRegistry

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post(
    "",
    response_model=ApiResponse[ImportJobResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_import_job(
    request: ImportJobCreateRequest,
    service: ImportJobService = Depends(get_import_job_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    job = await service.create(request, current_user.user_id)
    return ApiResponse.success(job.to_response(), message="Import job queued")


@router.get("", response_model=ApiResponse[ImportJobListResponse])
async def list_import_jobs(
    project_id: str = Query(..., description="Project ID"),
    active_only: bool = Query(False, description="Only queued/running jobs"),
    limit: int = Query(20, ge=1, le=100),
    service: ImportJobService = Depends(get_import_job_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    jobs = service.list_for_project(
        project_id,
        current_user.user_id,
        active_only=active_only,
        limit=limit,
    )
    return ApiResponse.success(
        ImportJobListResponse(
            jobs=[job.to_response() for job in jobs],
            total=len(jobs),
        ),
    )


@router.get("/providers", response_model=ApiResponse)
async def list_import_providers(
    registry: ProviderRegistry = Depends(get_import_provider_registry),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Authenticated capability discovery; no Project data or credentials."""
    return ApiResponse.success(import_specs(registry))


@router.get("/{job_id}", response_model=ApiResponse[ImportJobResponse])
async def get_import_job(
    job_id: str,
    service: ImportJobService = Depends(get_import_job_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    job = service.get_for_user(job_id, current_user.user_id)
    return ApiResponse.success(job.to_response())


@router.delete("/{job_id}", response_model=ApiResponse[ImportJobResponse])
async def cancel_import_job(
    job_id: str,
    service: ImportJobService = Depends(get_import_job_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    job = service.cancel(job_id, current_user.user_id)
    return ApiResponse.success(job.to_response(), message="Import job cancelled")
