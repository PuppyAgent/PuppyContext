"""Read-only Dashboard projection with domain-qualified identities and usage.

The legacy dashboard remains a bounded installed-client contract. This boundary
never joins Access and Synchronize by ID/path, and never turns a failed read into
an apparently empty inventory. Physical relation cutover belongs to ISSUE-049.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from src.common_schemas import ApiResponse
from src.infra.supabase.client import SupabaseClient
from src.platform.access.public_schemas import AccessKind
from src.platform.access.router import _redact_config
from src.platform.access.surface_repository import AccessSurfaceRepository
from src.platform.authorization.dependencies import AuthorizedProject, require_project_action
from src.platform.authorization.models import ProjectAction
from src.platform.project.dashboard_router import (
    DashboardNodeCounts,
    DashboardProject,
    DashboardTool,
    DashboardUpload,
    _compute_node_counts,
    _fetch_tools,
    _fetch_uploads,
)
from src.platform.query_contract import strict_query
from src.platform.repository_target.schemas import RepositoryTargetSchema
from src.platform.synchronize.repository import SynchronizeRepository
from src.platform.synchronize.router import _ensure_classified_binding
from src.platform.synchronize.run_repository import NEW_TABLE as SYNCHRONIZE_RUNS_TABLE
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.bootstrap.dependencies import get_product_operation_adapter


class DashboardResourceSummary(BaseModel):
    resource_id: str
    project_id: str
    name: str | None = None
    path: str | None = None
    direction: str | None = None
    status: str
    trigger: dict | None = None
    last_activity_at: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    usage_buckets: list[int] = Field(default_factory=list)


class DashboardSynchronizeBinding(DashboardResourceSummary):
    resource_kind: Literal["synchronize"] = "synchronize"
    provider: str
    path: str


class DashboardAccessSurface(DashboardResourceSummary):
    resource_kind: Literal["access"] = "access"
    kind: AccessKind
    target: RepositoryTargetSchema
    scope_mode: Literal["r", "rw"] | None = None


DashboardResource = Annotated[
    DashboardSynchronizeBinding | DashboardAccessSurface, Field(discriminator="resource_kind")
]


class ResourceDashboard(BaseModel):
    project: DashboardProject
    nodes: DashboardNodeCounts
    resources: list[DashboardResource]
    tools: list[DashboardTool]
    uploads: list[DashboardUpload]


def _usage(
    sb, table: str, key: str, ids: list[str], *, project_id: str | None = None, days: int = 14
):
    buckets = {identity: [0] * days for identity in ids}
    if not ids:
        return buckets
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    query = (
        sb.table(table)
        .select(f"{key}, started_at")
        .in_(key, ids)
        .gte("started_at", start.isoformat())
    )
    if project_id is not None:
        query = query.eq("project_id", project_id)
    for row in query.execute().data or []:
        identity = row.get(key)
        try:
            timestamp = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        index = (timestamp.astimezone(UTC).date() - start).days
        if identity in buckets and 0 <= index < days:
            buckets[identity][index] += 1
    return buckets


def fetch_dashboard_resources(sb, project_id: str) -> list[DashboardResource]:
    bindings = SynchronizeRepository(SimpleNamespace(client=sb)).list_by_project(project_id)
    for binding in bindings:
        _ensure_classified_binding(binding)
    access_repo = AccessSurfaceRepository(sb)
    surfaces = [
        row
        for row in access_repo.list_by_project(project_id)
        if ((row.get("config") or {}).get("trigger") or {}).get("type") != "import_once"
    ]
    scopes = access_repo.scope_rows_for(surfaces)
    # Distinct ID sets and log stores: equal IDs across domains cannot share runs.
    synchronize_usage = _usage(
        sb,
        SYNCHRONIZE_RUNS_TABLE,
        "connection_id",
        [row.id for row in bindings],
        project_id=project_id,
    )
    agent_usage = _usage(
        sb,
        "agent_execution_logs",
        "agent_id",
        [row["id"] for row in surfaces if row["kind"] == "agent"],
    )
    result: list[DashboardResource] = []
    for binding in bindings:
        if binding.project_id != project_id:
            raise HTTPException(409, "Dashboard returned a foreign Synchronize identity")
        result.append(
            DashboardSynchronizeBinding(
                resource_id=binding.id,
                project_id=project_id,
                provider=binding.provider,
                name=binding.config.get("name")
                or binding.config.get("source", {}).get("resource_name")
                or binding.provider,
                path=binding.path,
                direction=binding.direction,
                status=binding.status,
                trigger=_redact_config(binding.trigger),
                last_activity_at=binding.last_synced_at,
                error_message=binding.error_message,
                created_at=binding.created_at,
                usage_buckets=synchronize_usage[binding.id],
            )
        )
    for row in surfaces:
        if row["project_id"] != project_id:
            raise HTTPException(409, "Dashboard returned a foreign Access identity")
        scope_id = row.get("scope_id")
        scope = scopes.get(scope_id) if scope_id else None
        if scope_id and (
            not scope or scope.get("project_id") != project_id or not scope.get("path")
        ):
            raise HTTPException(409, "Access target requires repository Scope repair")
        target = (
            {"kind": "scope", "project_id": project_id, "scope_id": scope_id}
            if scope_id
            else {"kind": "project_root", "project_id": project_id}
        )
        config = row.get("config") or {}
        result.append(
            DashboardAccessSurface(
                resource_id=row["id"],
                project_id=project_id,
                kind=row["kind"],
                target=target,
                name=row.get("name"),
                path=scope["path"] if scope else "",
                status=row.get("status") or "active",
                direction=config.get("direction"),
                trigger=_redact_config(config.get("trigger")),
                last_activity_at=config.get("last_seen_at") or config.get("last_run_at"),
                error_message=config.get("error_message"),
                created_at=row.get("created_at"),
                scope_mode=scope.get("max_mode") if scope else None,
                usage_buckets=agent_usage.get(row["id"], [0] * 14)
                if row["kind"] == "agent"
                else [0] * 14,
            )
        )
    return result


router = APIRouter(prefix="/projects", tags=["projects"])


@router.get(
    "/{project_id}/dashboard/resources",
    response_model=ApiResponse[ResourceDashboard],
    dependencies=[strict_query()],
)
async def get_resource_dashboard(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_READ)),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project = authorized.project
    project_id = str(project.id)
    sb = SupabaseClient().client
    counts, resources, tools, uploads = await asyncio.gather(
        run_in_threadpool(_compute_node_counts, ops, project_id),
        run_in_threadpool(fetch_dashboard_resources, sb, project_id),
        run_in_threadpool(_fetch_tools, sb, project_id),
        run_in_threadpool(_fetch_uploads, sb, project_id),
    )
    return ApiResponse.success(
        ResourceDashboard(
            project=DashboardProject(
                id=project_id, name=project.name, description=project.description
            ),
            nodes=counts,
            resources=resources,
            tools=tools,
            uploads=uploads,
        )
    )
