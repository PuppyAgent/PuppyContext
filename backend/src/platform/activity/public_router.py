"""Typed read-only activity boundary. Historical text and resource IDs are unchanged."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.common_schemas import ApiResponse
from src.platform.activity.dependencies import get_activity_service
from src.platform.activity.schemas import ActivityItemResponse
from src.platform.activity.service import ActivityService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.query_contract import strict_query

ActivityKind = Literal["upload", "import", "synchronize_run"]


class ContextActivityItem(ActivityItemResponse):
    kind: ActivityKind


class ContextActivityList(BaseModel):
    items: list[ContextActivityItem]
    total: int


router = APIRouter(prefix="/activity", tags=["activity"])


@router.get(
    "/items",
    response_model=ApiResponse[ContextActivityList],
    dependencies=[strict_query("project_id", "kind", "active_only", "limit")],
)
def list_activity_items(
    project_id: str = Query(..., min_length=1),
    kind: ActivityKind | None = None,
    active_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    service: ActivityService = Depends(get_activity_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    rows = service.list_for_project(
        project_id,
        current_user.user_id,
        kind=kind,
        active_only=active_only,
        limit=limit,
    )
    items = [
        ContextActivityItem.model_validate(row.model_dump())
        for row in rows
    ]
    return ApiResponse.success(ContextActivityList(items=items, total=len(items)))
