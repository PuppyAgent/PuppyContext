"""Canonical-only HTTP boundary over the single Synchronize implementation.

Request, execution and history identities use the final resource contract all
the way to persistence. There is no legacy HTTP/DTO or storage fallback.
"""
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService
from src.platform.synchronize import router as operations
from src.platform.synchronize.arq_client import SyncArqClient
from src.platform.synchronize.dependencies import (
    get_sync_arq_client,
    get_synchronize_engine,
    get_synchronize_provider_registry,
    get_synchronize_service,
)
from src.platform.synchronize.engine import SynchronizeEngine
from src.platform.synchronize.public_schemas import (
    SynchronizeBinding,
    SynchronizeBindingCreate,
    SynchronizeBindingCreated,
    SynchronizeBindingUpdate,
    SynchronizeBootstrapResult,
    SynchronizeExecutionResult,
    SynchronizeFailedRun,
    SynchronizePullResult,
    SynchronizePushResult,
    SynchronizeRun,
    SynchronizeStatus,
    SynchronizeTriggerUpdate,
)
from src.platform.synchronize.service import SynchronizeService
from src.provider.registry import ProviderRegistry

Service = Annotated[SynchronizeService, Depends(get_synchronize_service)]
Registry = Annotated[ProviderRegistry, Depends(get_synchronize_provider_registry)]
Queue = Annotated[SyncArqClient, Depends(get_sync_arq_client)]
Engine = Annotated[SynchronizeEngine, Depends(get_synchronize_engine)]
Authorization = Annotated[AuthorizationService, Depends(get_authorization_service)]
User = Annotated[CurrentUser, Depends(get_current_user)]


def _validate_query_contract(request: Request) -> None:
    query = request.query_params
    forbidden = {"connection_id", "sync_id", "access_point_id", "target_folder_path"}
    if forbidden.intersection(query):
        raise HTTPException(422, "Use the canonical Synchronize resource fields; legacy identity parameters are not accepted.")
    if request.url.path.endswith("/synchronize/pull"):
        # A misspelled, empty or repeated selector must never silently become
        # a broader project-wide write. Other routes are already bound by ID.
        allowed = {"synchronize_binding_id", "project_id", "provider"}
        if set(query) - allowed or any(
            len(query.getlist(key)) != 1 or not query[key].strip() for key in query
        ):
            raise HTTPException(422, "Pull requires unambiguous, non-empty canonical selectors.")


router = APIRouter(prefix="/synchronize", tags=["synchronize"], dependencies=[Depends(_validate_query_contract)])


def _dict(value) -> dict:
    return value.model_dump() if isinstance(value, BaseModel) else dict(value)


def _reply(result: ApiResponse, project: Callable) -> ApiResponse:
    # Preserve unsuccessful envelopes (e.g. push source missing) verbatim.
    return ApiResponse(code=result.code, message=result.message,
                       data=project(result.data) if result.code == 0 and result.data is not None else result.data)


def _binding(value) -> SynchronizeBinding:
    fields = _dict(value)
    if not isinstance(fields.get("path"), str):
        raise HTTPException(409, "Synchronize target path requires repair; an absent path is not an explicit Project root.")
    return SynchronizeBinding(**fields)


def _execution(value) -> SynchronizeExecutionResult:
    return SynchronizeExecutionResult(**_dict(value))


def _created(value) -> SynchronizeBindingCreated:
    fields = _dict(value)
    return SynchronizeBindingCreated(binding=_binding(fields["binding"]),
        execution_result=_execution(fields["execution_result"]) if fields.get("execution_result") else None)


def _run(value) -> SynchronizeRun:
    return SynchronizeRun(**_dict(value))


def _failed_run(value) -> SynchronizeFailedRun:
    return SynchronizeFailedRun(**_dict(value))


def _pull(value) -> SynchronizePullResult:
    fields = _dict(value)
    return SynchronizePullResult(synced=fields["synced"], results=[_execution(row) for row in fields["results"]])


@router.get("/providers", response_model=ApiResponse)
def list_synchronize_providers(registry: Registry):
    return operations.list_connectors(registry=registry)


@router.get("/providers/{provider}/resources", response_model=ApiResponse[operations.ProviderResourcesResponse])
async def list_synchronize_provider_resources(provider: str, registry: Registry, current_user: User,
        q: str = "", cursor: str | None = None, resource_type: str | None = None):
    return await operations.list_provider_resources(provider=provider, q=q, cursor=cursor,
        resource_type=resource_type, registry=registry, current_user=current_user)


@router.get("/bindings", response_model=ApiResponse[list[SynchronizeBinding]])
def list_synchronize_bindings(project_id: str, service: Service, authorization: Authorization,
        current_user: User, provider: str | None = None):
    return _reply(operations.list_connections(project_id=project_id, provider=provider,
        service=service, authorization=authorization, current_user=current_user),
        lambda rows: [_binding(row) for row in rows])


@router.post("/bindings", response_model=ApiResponse[SynchronizeBindingCreated])
async def create_synchronize_binding(body: SynchronizeBindingCreate, service: Service, registry: Registry,
        sync_arq_client: Queue, authorization: Authorization, current_user: User):
    return _reply(await operations.create_connection(
        body=body, service=service, registry=registry,
        sync_arq_client=sync_arq_client, authorization=authorization, current_user=current_user), _created)


@router.patch("/bindings/{synchronize_binding_id}", response_model=ApiResponse[SynchronizeBinding])
async def update_synchronize_binding(synchronize_binding_id: str, body: SynchronizeBindingUpdate,
        service: Service, registry: Registry, authorization: Authorization, current_user: User):
    return _reply(await operations.update_connection(connection_id=synchronize_binding_id,
        body=body,
        service=service, registry=registry, authorization=authorization, current_user=current_user), _binding)


@router.delete("/bindings/{synchronize_binding_id}", response_model=ApiResponse)
async def delete_synchronize_binding(synchronize_binding_id: str, service: Service,
        authorization: Authorization, current_user: User):
    return await operations.delete_connection(connection_id=synchronize_binding_id, service=service,
        authorization=authorization, current_user=current_user)


@router.patch("/bindings/{synchronize_binding_id}/trigger", response_model=ApiResponse)
async def update_synchronize_trigger(synchronize_binding_id: str, body: SynchronizeTriggerUpdate,
        service: Service, authorization: Authorization, current_user: User):
    return await operations.update_connection_trigger(connection_id=synchronize_binding_id,
        body=body, service=service,
        authorization=authorization, current_user=current_user)


@router.post("/bindings/{synchronize_binding_id}/pause", response_model=ApiResponse)
def pause_synchronize_binding(synchronize_binding_id: str, service: Service,
        authorization: Authorization, current_user: User):
    return operations.pause_connection(connection_id=synchronize_binding_id, service=service,
        authorization=authorization, current_user=current_user)


@router.post("/bindings/{synchronize_binding_id}/resume", response_model=ApiResponse)
async def resume_synchronize_binding(synchronize_binding_id: str, service: Service, sync_arq_client: Queue,
        authorization: Authorization, current_user: User):
    return await operations.resume_connection(connection_id=synchronize_binding_id, service=service,
        sync_arq_client=sync_arq_client, authorization=authorization, current_user=current_user)


@router.post("/bindings/{synchronize_binding_id}/refresh", response_model=ApiResponse[SynchronizePullResult])
async def refresh_synchronize_binding(synchronize_binding_id: str, service: Service, sync_arq_client: Queue,
        authorization: Authorization, current_user: User):
    return _reply(await operations.refresh_connection(connection_id=synchronize_binding_id, service=service,
        sync_arq_client=sync_arq_client, authorization=authorization, current_user=current_user), _pull)


@router.get("/bindings/{synchronize_binding_id}/runs", response_model=ApiResponse[list[SynchronizeRun]])
def list_synchronize_runs(synchronize_binding_id: str, service: Service, authorization: Authorization,
        current_user: User, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return _reply(operations.list_connection_runs(connection_id=synchronize_binding_id, limit=limit,
        offset=offset, service=service, authorization=authorization, current_user=current_user),
        lambda rows: [_run(row) for row in rows])


@router.get("/runs/{run_id}", response_model=ApiResponse[SynchronizeRun])
def get_synchronize_run(run_id: str, service: Service, authorization: Authorization, current_user: User):
    return _reply(operations.get_connection_run(run_id=run_id, service=service,
        authorization=authorization, current_user=current_user), _run)


@router.get("/failed-runs", response_model=ApiResponse[list[SynchronizeFailedRun]])
def list_failed_synchronize_runs(project_id: str, service: Service, authorization: Authorization,
        current_user: User, limit: int = Query(50, ge=1, le=200)):
    return _reply(operations.list_failed_runs(project_id=project_id, limit=limit, service=service,
        authorization=authorization, current_user=current_user), lambda rows: [_failed_run(row) for row in rows])


@router.get("/status", response_model=ApiResponse[SynchronizeStatus])
async def get_synchronize_status(project_id: str, service: Service, authorization: Authorization, current_user: User):
    return _reply(await operations.get_project_sync_status(project_id=project_id, service=service,
        authorization=authorization, current_user=current_user),
        lambda data: SynchronizeStatus(**_dict(data)))


@router.post("/bootstrap", response_model=ApiResponse[SynchronizeBootstrapResult])
async def bootstrap_synchronize_bindings(body: SynchronizeBindingCreate, service: Service, registry: Registry,
        sync_arq_client: Queue, authorization: Authorization, current_user: User):
    return _reply(await operations.bootstrap(body=body,
        service=service, registry=registry, sync_arq_client=sync_arq_client, authorization=authorization,
        current_user=current_user), lambda data: SynchronizeBootstrapResult(**_dict(data)))


@router.post("/pull", response_model=ApiResponse[SynchronizePullResult])
async def pull_synchronize_bindings(service: Service, sync_arq_client: Queue, authorization: Authorization,
        current_user: User, synchronize_binding_id: str | None = None, project_id: str | None = None,
        provider: str | None = None):
    return _reply(await operations.trigger_pull(connection_id=synchronize_binding_id, project_id=project_id,
        provider=provider, service=service, sync_arq_client=sync_arq_client, authorization=authorization,
        current_user=current_user), _pull)


@router.post("/push/{path:path}", response_model=ApiResponse[SynchronizePushResult])
async def push_synchronize_path(path: str, project_id: str, engine: Engine,
        authorization: Authorization, current_user: User):
    return _reply(await operations.trigger_push(path=path, project_id=project_id, engine=engine,
        authorization=authorization, current_user=current_user),
        lambda data: SynchronizePushResult(pushed=_dict(data)["pushed"],
            results=[_execution(row) for row in _dict(data)["results"]]))
