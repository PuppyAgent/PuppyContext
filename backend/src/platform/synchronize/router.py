"""Integration API.

Integration is the top-level durable relationship resource. It writes through
the project-root Version Engine path, not Access scopes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.common_schemas import ApiResponse
from src.provider._base import AuthRequirement
from src.provider.registry import ProviderRegistry
from src.platform.synchronize.config_contract import (
    validate_bootstrap_config,
    validate_structured_config,
)
from src.platform.synchronize.schemas import (
    BootstrapResponse,
    CreateSyncResponse,
    FailedSyncRunItem,
    ProjectSyncStatusResponse,
    PullResponse,
    PushResponse,
    SyncResponse,
    SyncRunResponse,
    SyncStatusItem,
    connection_to_response,
)
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.synchronize.dependencies import (
    get_synchronize_provider_registry,
    get_synchronize_engine,
    get_synchronize_service,
    get_sync_arq_client,
)
from src.platform.synchronize.arq_client import SyncArqClient
from src.platform.synchronize.engine import SynchronizeEngine
from src.platform.synchronize.paths import canonical_provider
from src.platform.synchronize.service import SynchronizeService
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService
from src.platform.authorization.models import ProjectAction
from src.utils.logger import log_error


router = APIRouter(prefix="/integrations", tags=["integrations"])

_PROJECT_ID_DESC = "Project ID"
_PULL_DIRECTIONS = {"inbound", "bidirectional"}
_QUEUEABLE_PULL_STATUSES = {"active", "error"}


class BootstrapRequest(BaseModel):
    project_id: str
    provider: str
    config: dict
    target_folder_path: Optional[str] = None
    target_path: Optional[str] = None
    credentials_ref: Optional[str] = None
    direction: str = "inbound"
    conflict_strategy: str = "three_way_merge"
    sync_mode: str = "manual"
    trigger: Optional[dict] = None


class CreateIntegrationRequest(BaseModel):
    project_id: str
    provider: str
    config: dict
    target_folder_path: Optional[str] = None
    target_path: Optional[str] = None
    credentials_ref: Optional[str] = None
    direction: str = "inbound"
    conflict_strategy: str = "three_way_merge"
    sync_mode: str = "manual"
    trigger: Optional[dict] = None


class UpdateIntegrationTriggerRequest(BaseModel):
    sync_mode: str
    trigger: Optional[dict] = None


class UpdateIntegrationConnectionRequest(BaseModel):
    config: Optional[dict] = None
    target_path: Optional[str] = None
    direction: Optional[str] = None
    conflict_strategy: Optional[str] = None


class ProviderResourceItem(BaseModel):
    id: str
    type: str
    name: str
    url: Optional[str] = None
    subtitle: Optional[str] = None
    icon: Optional[str] = None
    authorized: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResourcesResponse(BaseModel):
    resources: list[ProviderResourceItem]
    next_cursor: Optional[str] = None


def _connectable_specs(registry: ProviderRegistry) -> list[dict]:
    from src.platform.synchronize.providers import synchronize_specs

    return synchronize_specs(registry)


def _ensure_project_access(
    authorization: AuthorizationService,
    current_user: CurrentUser,
    project_id: str,
    action=None,
) -> None:
    authorization.authorize(
        project_id,
        current_user.user_id,
        action or ProjectAction.ACCESS_READ,
    )


def _ensure_classified_binding(connection) -> None:
    # During 049's storage cutover, `connections` also contains Database Import
    # sources. These indicators are NOT a positive classification rule: block
    # ambiguous records until the migration proves ownership. Never expose
    # db_config or let binding management delete an Import source by accident.
    config = getattr(connection, "config", None) or {}
    if (getattr(connection, "provider", None) == "database" or "db_config" in config
            or (getattr(connection, "trigger", None) or {}).get("type") == "import_once"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SOURCE_CLASSIFICATION_REQUIRED",
                "message": "Classify and migrate legacy source records before Synchronize management. Import source IDs must not be reused as binding IDs.",
            },
        )


def _get_connection_with_access(
    *,
    connection_id: str,
    service: SynchronizeService,
    authorization: AuthorizationService,
    current_user: CurrentUser,
    action=None,
):
    connection = service.repository.get_by_id(connection_id)
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Integration connection #{connection_id} not found",
        )
    _ensure_project_access(
        authorization, current_user, connection.project_id, action
    )
    _ensure_classified_binding(connection)
    return connection


def _sync_resp(connection) -> SyncResponse:
    return SyncResponse(**connection_to_response(connection))


def _target_from_request(body: CreateIntegrationRequest | BootstrapRequest) -> str | None:
    return body.target_path or body.target_folder_path


def _ensure_direction_supported(direction: str, spec) -> None:
    supported = set(spec.supported_directions or [])
    if direction in supported:
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"Direction {direction!r} is not supported by {spec.provider}. "
            f"Supported directions: {', '.join(spec.supported_directions)}"
        ),
    )


def _can_queue_pull(connection) -> bool:
    return (
        connection.direction in _PULL_DIRECTIONS
        and connection.status in _QUEUEABLE_PULL_STATUSES
    )


def _ensure_pull_queueable(connection) -> None:
    if connection.direction not in _PULL_DIRECTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This integration is not configured for inbound sync.",
        )
    if connection.status not in _QUEUEABLE_PULL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Integration cannot be synced while status is {connection.status}.",
        )


def _get_run_repo():
    from src.platform.synchronize.run_repository import SyncRunRepository
    from src.infra.supabase.client import SupabaseClient

    return SyncRunRepository(SupabaseClient())


def _queued_run_result(*, connection, run, created: bool) -> dict:
    return {
        "connection_id": connection.id,
        "access_point_id": connection.id,
        "run_id": run.id,
        "worker_job_id": run.worker_job_id,
        "path": connection.path,
        "provider": connection.provider,
        "status": run.status,
        "summary": "Sync queued" if created else f"Sync already {run.status}",
        "deduped": not created,
    }


async def _queue_sync_run(
    *,
    connection,
    trigger_type: str,
    sync_arq_client: SyncArqClient,
) -> dict:
    run_repo = _get_run_repo()
    active_run = run_repo.get_blocking_active_by_sync(connection.id)
    if active_run:
        return _queued_run_result(
            connection=connection,
            run=active_run,
            created=False,
        )

    _ensure_pull_queueable(connection)
    run, created = run_repo.create_queued_single_lane(
        connection.id,
        trigger_type=trigger_type,
    )
    if not created:
        return _queued_run_result(
            connection=connection,
            run=run,
            created=False,
        )

    try:
        worker_job_id = await sync_arq_client.enqueue_sync_run(run.id)
        run_repo.set_worker_job_id(run.id, worker_job_id)
        run.worker_job_id = worker_job_id
    except Exception as exc:
        run_repo.complete(
            run.id,
            status="failed",
            error=f"Failed to enqueue sync worker: {exc}",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sync worker is unavailable. Try again in a moment.",
        ) from exc
    return _queued_run_result(connection=connection, run=run, created=True)


@router.get("/status", response_model=ApiResponse[ProjectSyncStatusResponse])
async def get_project_sync_status(
    project_id: str = Query(..., description=_PROJECT_ID_DESC),
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    _ensure_project_access(authorization, current_user, project_id)
    connections = service.repository.list_by_project(project_id)
    for connection in connections:
        _ensure_classified_binding(connection)
    items = [
        SyncStatusItem(
            id=c.id,
            path=c.path,
            node_name=c.path.rsplit("/", 1)[-1] if c.path else None,
            node_type=None,
            provider=c.provider,
            direction=c.direction,
            status=c.status,
            name=((c.config or {}).get("source") or {}).get("resource_name"),
            access_key=None,
            trigger=c.trigger if c.trigger else None,
            last_synced_at=c.last_synced_at,
            error_message=c.error_message,
        )
        for c in connections
    ]
    return ApiResponse.success(
        data=ProjectSyncStatusResponse(syncs=items, uploads=[])
    )


@router.get("/connectors", response_model=ApiResponse)
def list_connectors(
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
):
    return ApiResponse.success(data=_connectable_specs(registry))


@router.get("/providers/{provider}/resources", response_model=ApiResponse[ProviderResourcesResponse])
async def list_provider_resources(
    provider: str,
    q: str = Query("", description="Optional provider resource search term"),
    cursor: Optional[str] = Query(None, description="Provider pagination cursor"),
    resource_type: Optional[str] = Query(None, description="Provider resource type filter"),
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
    current_user: CurrentUser = Depends(get_current_user),
):
    canonical = canonical_provider(provider)
    connector = registry.get(canonical)
    if not connector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown integration provider: {provider}",
        )

    spec = connector.spec()
    try:
        credentials = await registry.resolve_credentials(
            oauth_type=spec.oauth_type,
            user_id=current_user.user_id,
            required=spec.auth != AuthRequirement.OPTIONAL_OAUTH,
        )
        resources, next_cursor = await connector.list_source_resources(
            credentials,
            query=q,
            cursor=cursor,
            resource_type=resource_type,
        )
    except ValueError as exc:
        log_error(f"[Integrations] list_source_resources failed provider={provider}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to authorize with the provider",
        ) from exc

    return ApiResponse.success(
        data=ProviderResourcesResponse(
            resources=[
                ProviderResourceItem(
                    id=item.id,
                    type=item.type,
                    name=item.name,
                    url=item.url,
                    subtitle=item.subtitle,
                    icon=item.icon,
                    authorized=item.authorized,
                    metadata=item.metadata,
                )
                for item in resources
            ],
            next_cursor=next_cursor,
        )
    )


@router.post("/connections", response_model=ApiResponse[CreateSyncResponse])
async def create_connection(
    body: CreateIntegrationRequest,
    service: SynchronizeService = Depends(get_synchronize_service),
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
    sync_arq_client: SyncArqClient = Depends(get_sync_arq_client),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    from src.platform.authorization.models import ProjectAction
    _ensure_project_access(
        authorization,
        current_user,
        body.project_id,
        ProjectAction.SYNCHRONIZE_MANAGE,
    )

    provider = canonical_provider(body.provider)
    connector = registry.get(provider)
    if not connector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown integration provider: {body.provider}",
        )
    if body.sync_mode == "import_once":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One-time imports must use ImportJob, not Integration.",
        )
    if connector.spec().creation_mode != "direct":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AccessSurface {provider} must be created via bootstrap",
        )
    _ensure_direction_supported(body.direction, connector.spec())

    try:
        config = registry.pin_materialization_schema(provider, body.config)
        validate_structured_config(provider, connector.spec(), config)
        connection = await service.create_connection(
            project_id=body.project_id,
            provider=provider,
            config=config,
            target_path=_target_from_request(body),
            credentials_ref=body.credentials_ref,
            direction=body.direction,
            conflict_strategy=body.conflict_strategy,
            sync_mode=body.sync_mode,
            trigger=body.trigger,
            user_id=current_user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    execution_result = None
    if connection.direction in _PULL_DIRECTIONS:
        try:
            execution_result = await _queue_sync_run(
                connection=connection,
                trigger_type="initial",
                sync_arq_client=sync_arq_client,
            )
        except HTTPException:
            service.remove_sync(connection.id)
            raise
        except Exception as exc:
            service.remove_sync(connection.id)
            log_error(f"[IntegrationCreate] First sync enqueue failed for {connection.id}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sync worker is unavailable. Try again in a moment.",
            ) from exc

    if execution_result and body.sync_mode == "scheduled" and body.trigger:
        try:
            from src.infra.scheduler.service import get_scheduler_service

            await get_scheduler_service().sync_trigger(
                connection_id=connection.id,
                provider=provider,
                trigger_config=body.trigger,
            )
        except Exception:
            pass

    refreshed = service.repository.get_by_id(connection.id) or connection
    return ApiResponse.success(
        data=CreateSyncResponse(
            sync=_sync_resp(refreshed),
            execution_result=execution_result,
        )
    )


@router.get("/connections", response_model=ApiResponse[list[SyncResponse]])
def list_connections(
    project_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required",
        )
    _ensure_project_access(authorization, current_user, project_id)
    if provider:
        connections = service.repository.list_by_provider(project_id, provider)
    else:
        connections = service.repository.list_by_project(project_id)
    for connection in connections:
        _ensure_classified_binding(connection)
    return ApiResponse.success(data=[_sync_resp(c) for c in connections])


@router.delete("/connections/{connection_id}", response_model=ApiResponse)
async def delete_connection(
    connection_id: str,
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
        action=ProjectAction.SYNCHRONIZE_MANAGE,
    )
    try:
        from src.infra.scheduler.service import get_scheduler_service

        await get_scheduler_service().sync_trigger(connection_id)
    except Exception:
        pass
    service.remove_sync(connection_id)
    return ApiResponse.success(message="Integration connection deleted")


@router.patch("/connections/{connection_id}", response_model=ApiResponse[SyncResponse])
async def update_connection(
    connection_id: str,
    body: UpdateIntegrationConnectionRequest,
    service: SynchronizeService = Depends(get_synchronize_service),
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    connection = _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
        action=ProjectAction.SYNCHRONIZE_MANAGE,
    )
    fields: dict[str, Any] = {}
    if body.direction is not None:
        connector = registry.get(connection.provider)
        if connector:
            _ensure_direction_supported(body.direction, connector.spec())
        fields["direction"] = body.direction
    if body.target_path is not None:
        fields["target_path"] = body.target_path
    if body.conflict_strategy is not None:
        fields["conflict_strategy"] = body.conflict_strategy
    if body.config:
        # Only merge product/provider config. System-owned identifiers remain
        # owned by create/bootstrap/OAuth flows, not editable workflow settings.
        blocked = {
            "external_resource_id",
            "materialization_schema",
            "user_id",
            "credentials_ref",
            "access_key",
        }
        fields.update({
            key: value
            for key, value in body.config.items()
            if key not in blocked
        })
        connector = registry.get(connection.provider)
        if connector:
            merged_config = dict(connection.config or {})
            merged_config.update({
                key: value
                for key, value in body.config.items()
                if key not in blocked
            })
            try:
                validate_structured_config(
                    connection.provider,
                    connector.spec(),
                    merged_config,
                    allow_system_keys=True,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc
    if fields:
        service.repository.update(connection_id, **fields)
    refreshed = service.repository.get_by_id(connection_id) or connection
    return ApiResponse.success(data=_sync_resp(refreshed))


@router.patch("/connections/{connection_id}/trigger", response_model=ApiResponse)
async def update_connection_trigger(
    connection_id: str,
    body: UpdateIntegrationTriggerRequest,
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    if body.sync_mode == "import_once":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One-time imports must use ImportJob, not Integration.",
        )
    connection = _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
        action=ProjectAction.SYNCHRONIZE_MANAGE,
    )
    trigger_data = dict(body.trigger or {})
    if not trigger_data.get("type"):
        trigger_data["type"] = body.sync_mode
    try:
        service.update_trigger(connection_id, mode=body.sync_mode, trigger=trigger_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        from src.infra.scheduler.service import get_scheduler_service

        await get_scheduler_service().sync_trigger(
            connection_id=connection_id,
            provider=connection.provider,
            trigger_config=trigger_data if body.sync_mode == "scheduled" else None,
        )
    except Exception:
        pass
    return ApiResponse.success(message=f"Integration trigger updated to {body.sync_mode}")


@router.post("/connections/{connection_id}/pause", response_model=ApiResponse)
def pause_connection(
    connection_id: str,
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
        action=ProjectAction.SYNCHRONIZE_MANAGE,
    )
    service.pause_sync(connection_id)
    return ApiResponse.success(message="Integration paused")


@router.post("/connections/{connection_id}/refresh", response_model=ApiResponse[PullResponse])
async def refresh_connection(
    connection_id: str,
    service: SynchronizeService = Depends(get_synchronize_service),
    sync_arq_client: SyncArqClient = Depends(get_sync_arq_client),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    connection = _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
        action=ProjectAction.AUTOMATION_RUN,
    )
    if (connection.trigger or {}).get("type") == "import_once":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot refresh an import-once job.",
        )
    result = await _queue_sync_run(
        connection=connection,
        trigger_type="manual",
        sync_arq_client=sync_arq_client,
    )
    return ApiResponse.success(data=PullResponse(synced=0, results=[result]))


@router.post("/connections/{connection_id}/resume", response_model=ApiResponse)
async def resume_connection(
    connection_id: str,
    service: SynchronizeService = Depends(get_synchronize_service),
    sync_arq_client: SyncArqClient = Depends(get_sync_arq_client),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    connection = _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
        action=ProjectAction.SYNCHRONIZE_MANAGE,
    )
    service.resume_sync(connection_id)
    connection = service.repository.get_by_id(connection_id) or connection
    await _queue_sync_run(
        connection=connection,
        trigger_type="manual",
        sync_arq_client=sync_arq_client,
    )
    return ApiResponse.success(message="Integration resumed")


@router.get("/failed-runs", response_model=ApiResponse[list[FailedSyncRunItem]])
def list_failed_runs(
    project_id: str = Query(..., description=_PROJECT_ID_DESC),
    limit: int = Query(50, ge=1, le=200),
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    _ensure_project_access(authorization, current_user, project_id)
    connections = service.repository.list_by_project(project_id)
    if not connections:
        return ApiResponse.success(data=[])
    for connection in connections:
        _ensure_classified_binding(connection)
    by_id = {c.id: c for c in connections}
    runs = _get_run_repo().list_failed_for_connections(list(by_id), limit=limit)
    items = []
    for run in runs:
        connection = by_id.get(run.connection_id)
        source = (connection.config or {}).get("source") if connection else {}
        items.append(FailedSyncRunItem(
            id=run.id,
            access_point_id=run.connection_id,
            access_point_name=source.get("resource_name") if isinstance(source, dict) else None,
            access_point_path=connection.path if connection else None,
            provider=connection.provider if connection else "",
            direction=connection.direction if connection else "",
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=run.duration_ms,
            error=run.error,
            result_summary=run.result_summary,
            trigger_type=run.trigger_type,
        ))
    return ApiResponse.success(data=items)


@router.get("/connections/{connection_id}/runs", response_model=ApiResponse[list[SyncRunResponse]])
def list_connection_runs(
    connection_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    _get_connection_with_access(
        connection_id=connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
    )
    runs = _get_run_repo().list_by_sync(connection_id, limit=limit, offset=offset)
    return ApiResponse.success(data=[
        SyncRunResponse(
            id=r.id,
            access_point_id=r.connection_id,
            status=r.status,
            worker_job_id=r.worker_job_id,
            started_at=r.started_at,
            finished_at=r.finished_at,
            duration_ms=r.duration_ms,
            exit_code=r.exit_code,
            error=r.error,
            trigger_type=r.trigger_type,
            result_summary=r.result_summary,
        )
        for r in runs
    ])


@router.get("/runs/{run_id}", response_model=ApiResponse[SyncRunResponse])
def get_connection_run(
    run_id: str,
    service: SynchronizeService = Depends(get_synchronize_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    run = _get_run_repo().get_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # Scope the read to a project the caller can reach — a run id alone must not
    # leak another tenant's sync output. Resolve the owning connection and
    # enforce project access (mirrors list_connection_runs).
    _get_connection_with_access(
        connection_id=run.connection_id,
        service=service,
        authorization=authorization,
        current_user=current_user,
    )
    return ApiResponse.success(data=SyncRunResponse(
        id=run.id,
        access_point_id=run.connection_id,
        status=run.status,
        worker_job_id=run.worker_job_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        exit_code=run.exit_code,
        stdout=run.stdout,
        error=run.error,
        trigger_type=run.trigger_type,
        result_summary=run.result_summary,
    ))


@router.post("/bootstrap", response_model=ApiResponse[BootstrapResponse])
async def bootstrap(
    body: BootstrapRequest,
    service: SynchronizeService = Depends(get_synchronize_service),
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
    sync_arq_client: SyncArqClient = Depends(get_sync_arq_client),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    from src.platform.authorization.models import ProjectAction
    _ensure_project_access(
        authorization,
        current_user,
        body.project_id,
        ProjectAction.SYNCHRONIZE_MANAGE,
    )
    provider = canonical_provider(body.provider)
    connector = registry.get(provider)
    if not connector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown integration provider: {body.provider}",
        )
    if connector.spec().creation_mode != "bootstrap":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AccessSurface {provider} must be created via POST /integrations/connections",
        )
    _ensure_direction_supported(body.direction, connector.spec())

    try:
        config = registry.pin_materialization_schema(provider, body.config)
        validate_bootstrap_config(config)
        connections = await service.bootstrap(
            project_id=body.project_id,
            provider=provider,
            config=config,
            target_folder_path=_target_from_request(body),
            credentials_ref=body.credentials_ref,
            direction=body.direction,
            conflict_strategy=body.conflict_strategy,
            sync_mode=body.sync_mode,
            trigger=body.trigger,
            user_id=current_user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    queued_connections = []
    for connection in connections:
        if connection.direction not in _PULL_DIRECTIONS:
            continue
        try:
            await _queue_sync_run(
                connection=connection,
                trigger_type="initial",
                sync_arq_client=sync_arq_client,
            )
            queued_connections.append(connection)
        except Exception as exc:
            log_error(f"[IntegrationBootstrap] First sync enqueue failed for {connection.id}: {exc}")

    if body.sync_mode == "scheduled" and body.trigger:
        try:
            from src.infra.scheduler.service import get_scheduler_service

            scheduler = get_scheduler_service()
            for connection in queued_connections:
                await scheduler.sync_trigger(
                    connection_id=connection.id,
                    provider=provider,
                    trigger_config=body.trigger,
                )
        except Exception:
            pass

    return ApiResponse.success(data=BootstrapResponse(syncs_created=len(connections)))


@router.post("/pull", response_model=ApiResponse[PullResponse])
async def trigger_pull(
    connection_id: Optional[str] = Query(None, description="Connection ID. Omit to pull all."),
    project_id: Optional[str] = Query(None, description=_PROJECT_ID_DESC),
    provider: Optional[str] = Query(None),
    service: SynchronizeService = Depends(get_synchronize_service),
    sync_arq_client: SyncArqClient = Depends(get_sync_arq_client),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    if connection_id:
        connection = _get_connection_with_access(
            connection_id=connection_id,
            service=service,
            authorization=authorization,
            current_user=current_user,
            action=ProjectAction.AUTOMATION_RUN,
        )
        result = await _queue_sync_run(
            connection=connection,
            trigger_type="manual",
            sync_arq_client=sync_arq_client,
        )
        results = [result]
    else:
        if not project_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="project_id is required when connection_id is omitted",
            )
        _ensure_project_access(
            authorization,
            current_user,
            project_id,
            ProjectAction.AUTOMATION_RUN,
        )
        connections = (
            service.repository.list_by_provider(project_id, provider)
            if provider
            else service.repository.list_by_project(project_id)
        )
        # Validate the whole batch before enqueueing any work.
        for connection in connections:
            _ensure_classified_binding(connection)
        results = []
        for connection in connections:
            if connection.direction not in _PULL_DIRECTIONS:
                continue
            try:
                results.append(await _queue_sync_run(
                    connection=connection,
                    trigger_type="manual",
                    sync_arq_client=sync_arq_client,
                ))
            except HTTPException as exc:
                if exc.status_code in {
                    status.HTTP_400_BAD_REQUEST,
                    status.HTTP_409_CONFLICT,
                }:
                    continue
                raise
    return ApiResponse.success(data=PullResponse(synced=len(results), results=results))


@router.post("/push/{path:path}", response_model=ApiResponse[PushResponse])
async def trigger_push(
    path: str,
    project_id: str = Query(..., description=_PROJECT_ID_DESC),
    engine: SynchronizeEngine = Depends(get_synchronize_engine),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
    from src.version_engine.read.tree_reader import detect_type
    import json as _json

    from src.platform.authorization.models import ProjectAction
    _ensure_project_access(
        authorization,
        current_user,
        project_id,
        ProjectAction.AUTOMATION_RUN,
    )

    ops = build_worker_version_engine_container().product_operations()
    try:
        content = ops.read_file(project_id, path)
    except FileNotFoundError:
        return ApiResponse.error(code=1004, message=f"File not found: {path}")

    node_type = detect_type(path)
    if node_type == "json":
        try:
            parsed_content: Any = _json.loads(content.decode("utf-8"))
        except ValueError:
            parsed_content = content.decode("utf-8", errors="replace")
    else:
        parsed_content = content.decode("utf-8", errors="replace")

    result = await engine.push_execute(
        path=path,
        commit_id=ops.get_head_commit_id(project_id),
        content=parsed_content,
        node_type=node_type,
    )
    return ApiResponse.success(data=PushResponse(
        pushed=1 if result else 0,
        results=[result] if result else [],
    ))
