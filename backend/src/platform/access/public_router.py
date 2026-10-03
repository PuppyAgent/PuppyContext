"""Canonical Access HTTP boundary over the existing Access application paths."""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.common_schemas import ApiResponse
from src.platform.access import project_router as project_ops
from src.platform.access import router as global_ops
from src.platform.access.models import ACCESS_KINDS
from src.platform.access.public_schemas import (
    AccessCredentialIssued,
    AccessDirection,
    AccessKind,
    AccessSurface,
    AccessSurfaceConfigure,
    AccessSurfaceCreate,
    AccessSurfaceCreated,
    AccessSurfaceKind,
    AccessSurfaceMetadataUpdate,
    AccessSurfaceRename,
    AccessSurfaceUpdate,
    AccessTargetEnable,
)
from src.platform.access.service import AccessService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import (
    AuthorizedProject,
    get_authorization_service,
    require_project_action,
)
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.entitlements.dependencies import get_entitlement_service
from src.platform.entitlements.service import EntitlementService
from src.platform.repository_target.protocol import require_repository_target_contract

User = Annotated[CurrentUser, Depends(get_current_user)]
Authorization = Annotated[AuthorizationService, Depends(get_authorization_service)]
Service = Annotated[AccessService, Depends(project_ops.get_access_service)]
Entitlements = Annotated[EntitlementService, Depends(get_entitlement_service)]
ReadProject = Annotated[
    AuthorizedProject, Depends(require_project_action(ProjectAction.ACCESS_READ))
]
ManageProject = Annotated[
    AuthorizedProject, Depends(require_project_action(ProjectAction.ACCESS_MANAGE))
]
AgentProject = Annotated[
    AuthorizedProject, Depends(require_project_action(ProjectAction.AGENT_MANAGE))
]


def query_contract(*allowed: str):
    def validate(request: Request):
        query = request.query_params
        if set(query) - set(allowed) or any(
            len(query.getlist(key)) != 1 or not query[key].strip() for key in query
        ):
            raise HTTPException(422, "Use non-empty, unambiguous canonical Access selectors.")

    return Depends(validate)


router = APIRouter(
    prefix="/access/surfaces",
    tags=["access"],
    dependencies=[Depends(require_repository_target_contract)],
)
project_router = APIRouter(
    prefix="/projects/{project_id}/access/surfaces",
    tags=["access"],
    dependencies=[Depends(require_repository_target_contract)],
)


def _fields(value) -> dict:
    return value.model_dump() if isinstance(value, BaseModel) else dict(value)


def _is_access(fields: dict) -> bool:
    return (
        fields.get("provider") in ACCESS_KINDS
        and (fields.get("trigger") or {}).get("type") != "import_once"
    )


def _require_access(fields: dict) -> None:
    if not _is_access(fields):
        raise HTTPException(
            409,
            "Legacy source records are not ongoing Access surfaces. Review their migration before management; use Import or Synchronize for source operations.",
        )


def _surface(value) -> AccessSurface:
    fields = _fields(value)
    _require_access(fields)
    target = fields["target"]
    project_id = fields.get("project_id", target["project_id"])
    if project_id != target["project_id"]:
        raise ValueError("Access surface and target Project identities disagree")
    # Explicit envelope projection, not a recursive resource-name replacement.
    fields["project_id"] = project_id
    fields["kind"] = fields.pop("provider")
    fields["last_activity_at"] = fields.pop("last_synced_at", fields.pop("last_run_at", None))
    fields.pop("last_run_id", None)
    fields["config"] = global_ops._redact_config(fields.get("config") or {})
    fields["policy"] = global_ops._redact_config(fields.get("policy") or {})
    fields["trigger"] = global_ops._redact_config(fields.get("trigger"))
    return AccessSurface(**fields)


def _reply(
    result: ApiResponse, project: Callable, message="Access operation completed"
) -> ApiResponse:
    return ApiResponse(
        code=result.code,
        message=message if result.code == 0 else result.message,
        data=project(result.data) if result.code == 0 and result.data is not None else result.data,
    )


def _safe_metadata(*values) -> None:
    if any(global_ops._contains_secret_config_key(value) for value in values):
        raise HTTPException(
            400,
            "Credentials cannot be written through Access metadata; use explicit credential issuance.",
        )


def _project_surface(surface_id: str, authorized: AuthorizedProject, service: AccessService):
    surface = service.get(surface_id)
    if surface is None or surface.project_id != str(authorized.project.id):
        raise HTTPException(404, "Access surface not found")
    _require_access({"provider": surface.kind, "trigger": surface.trigger})
    return surface


def _global_surface(
    surface_id: str,
    current_user: CurrentUser,
    authorization: AuthorizationService,
    service: AccessService,
    action: ProjectAction,
):
    surface = service.get(surface_id)
    if surface is None:
        raise HTTPException(404, "Access surface not found")
    authorization.authorize(surface.project_id, current_user.user_id, action)
    _require_access({"provider": surface.kind, "trigger": surface.trigger})
    return surface


@router.get(
    "",
    response_model=ApiResponse[list[AccessSurface]],
    dependencies=[query_contract("project_id", "kind", "status")],
)
def list_access_surfaces(
    current_user: User,
    authorization: Authorization,
    project_id: str | None = None,
    kind: AccessKind | None = None,
    status: str | None = None,
):
    return _reply(
        global_ops.list_connections(
            project_id=project_id,
            provider=kind,
            connection_status=status,
            current_user=current_user,
            authorization=authorization,
        ),
        lambda rows: [_surface(row) for row in rows if _is_access(_fields(row))],
        "Access surfaces listed",
    )


# Static route must precede the resource-ID route; both precede legacy /access/{id}.
@router.get(
    "/types", response_model=ApiResponse[list[AccessSurfaceKind]], dependencies=[query_contract()]
)
def list_access_surface_types():
    return _reply(
        global_ops.list_connection_types(),
        lambda rows: [
            AccessSurfaceKind(
                kind=row["provider"], **{k: v for k, v in row.items() if k != "provider"}
            )
            for row in rows
        ],
    )


@router.post(
    "",
    response_model=ApiResponse[AccessSurfaceCreated],
    status_code=201,
    dependencies=[query_contract()],
)
async def configure_access_surface(
    body: AccessSurfaceConfigure,
    current_user: User,
    authorization: Authorization,
    entitlements: Entitlements,
):
    authorization.authorize(body.project_id, current_user.user_id, ProjectAction.ACCESS_MANAGE)
    _safe_metadata(body.config, body.accesses, body.tools_config)
    fields = body.model_dump(exclude={"kind"})
    result = await global_ops.create_connection(
        payload=global_ops.UnifiedConnectionCreate(provider=body.kind, **fields),
        current_user=current_user,
        entitlement_service=entitlements,
        authorization=authorization,
    )
    return _reply(
        result,
        lambda data: AccessSurfaceCreated(
            kind=data.provider, **data.model_dump(exclude={"provider"})
        ),
        "Access surface created",
    )


@router.get(
    "/{access_surface_id}",
    response_model=ApiResponse[AccessSurface],
    dependencies=[query_contract()],
)
def get_access_surface(access_surface_id: str, current_user: User, authorization: Authorization):
    return _reply(
        global_ops.get_connection(
            connection_id=access_surface_id, current_user=current_user, authorization=authorization
        ),
        _surface,
    )


@router.patch(
    "/{access_surface_id}",
    response_model=ApiResponse[AccessSurface],
    dependencies=[query_contract()],
)
async def update_access_surface_metadata(
    access_surface_id: str,
    body: AccessSurfaceMetadataUpdate,
    current_user: User,
    authorization: Authorization,
    service: Service,
):
    _global_surface(
        access_surface_id, current_user, authorization, service, ProjectAction.ACCESS_MANAGE
    )
    _safe_metadata(body.config, body.trigger.model_dump() if body.trigger else None)
    service.update(access_surface_id, body.model_dump(exclude_unset=True, exclude_none=True))
    return get_access_surface(access_surface_id, current_user, authorization)


@router.delete(
    "/{access_surface_id}", response_model=ApiResponse[None], dependencies=[query_contract()]
)
async def delete_access_surface(
    access_surface_id: str, current_user: User, authorization: Authorization, service: Service
):
    _global_surface(
        access_surface_id, current_user, authorization, service, ProjectAction.ACCESS_MANAGE
    )
    return await global_ops.delete_connection(
        connection_id=access_surface_id, current_user=current_user, authorization=authorization
    )


@router.patch(
    "/{access_surface_id}/rename",
    response_model=ApiResponse[AccessSurface],
    dependencies=[query_contract()],
)
def rename_access_surface(
    access_surface_id: str,
    body: AccessSurfaceRename,
    current_user: User,
    authorization: Authorization,
    service: Service,
):
    _global_surface(
        access_surface_id, current_user, authorization, service, ProjectAction.ACCESS_MANAGE
    )
    return _reply(
        global_ops.rename_connection(
            connection_id=access_surface_id,
            body=body.model_dump(),
            current_user=current_user,
            authorization=authorization,
        ),
        _surface,
    )


@router.post(
    "/{access_surface_id}/regenerate-key",
    response_model=ApiResponse[AccessCredentialIssued],
    dependencies=[query_contract()],
)
def regenerate_access_surface_key(
    access_surface_id: str, current_user: User, authorization: Authorization, service: Service
):
    _global_surface(
        access_surface_id, current_user, authorization, service, ProjectAction.CREDENTIAL_MANAGE
    )
    result = global_ops.regenerate_key(
        connection_id=access_surface_id, current_user=current_user, authorization=authorization
    )
    return _reply(
        result,
        lambda data: AccessCredentialIssued(
            access_surface_id=access_surface_id,
            credential=data.get("credential") or data["access_key"],
            target=data.get("target"),
            credential_hint=data.get("access_key_hint"),
        ),
    )


@project_router.get(
    "",
    response_model=ApiResponse[list[AccessSurface]],
    dependencies=[query_contract("kind", "direction")],
)
def list_project_access_surfaces(
    authorized: ReadProject,
    service: Service,
    kind: AccessKind | None = None,
    direction: AccessDirection | None = None,
):
    return _reply(
        project_ops.list_connectors(
            provider=kind,
            direction=direction,
            include_non_access=False,
            authorized=authorized,
            service=service,
        ),
        lambda rows: [_surface(row) for row in rows],
        "Access surfaces listed",
    )


@project_router.post(
    "", response_model=ApiResponse[AccessSurface], status_code=201, dependencies=[query_contract()]
)
def create_project_access_surface(
    body: AccessSurfaceCreate, authorized: ManageProject, current_user: User, service: Service
):
    _safe_metadata(body.config, body.policy, body.trigger.model_dump())
    return _reply(
        project_ops.create_connector(
            payload=project_ops.ConnectorIn(
                provider=body.kind, **body.model_dump(exclude={"kind"})
            ),
            authorized=authorized,
            current_user=current_user,
            service=service,
        ),
        _surface,
        "Access surface created",
    )


@project_router.post(
    "/enable-target",
    response_model=ApiResponse[list[AccessSurface]],
    dependencies=[query_contract()],
)
def enable_target_access_surfaces(
    body: AccessTargetEnable, authorized: ManageProject, current_user: User, service: Service
):
    return _reply(
        project_ops.enable_target_access(
            payload=project_ops.TargetAccessEnableIn(**body.model_dump()),
            authorized=authorized,
            current_user=current_user,
            service=service,
        ),
        lambda rows: [_surface(row) for row in rows],
    )


@project_router.patch(
    "/{access_surface_id}",
    response_model=ApiResponse[AccessSurface],
    dependencies=[query_contract()],
)
def update_project_access_surface(
    access_surface_id: str, body: AccessSurfaceUpdate, authorized: ManageProject, service: Service
):
    _project_surface(access_surface_id, authorized, service)
    _safe_metadata(body.config, body.policy, body.trigger.model_dump() if body.trigger else None)
    return _reply(
        project_ops.update_connector(
            connector_id=access_surface_id,
            payload=project_ops.ConnectorPatch(**body.model_dump(exclude_unset=True)),
            authorized=authorized,
            service=service,
        ),
        _surface,
    )


@project_router.post(
    "/{access_surface_id}/activate-agent",
    response_model=ApiResponse[AccessSurface],
    dependencies=[query_contract()],
)
def activate_access_surface_agent(
    access_surface_id: str, authorized: AgentProject, service: Service
):
    _project_surface(access_surface_id, authorized, service)
    return _reply(
        project_ops.activate_agent_connector(
            connector_id=access_surface_id, authorized=authorized, service=service
        ),
        _surface,
    )


@project_router.post(
    "/{access_surface_id}/pause", response_model=ApiResponse[None], dependencies=[query_contract()]
)
def pause_access_surface(access_surface_id: str, authorized: ManageProject, service: Service):
    _project_surface(access_surface_id, authorized, service)
    return project_ops.pause_connector(
        connector_id=access_surface_id, authorized=authorized, service=service
    )


@project_router.post(
    "/{access_surface_id}/resume", response_model=ApiResponse[None], dependencies=[query_contract()]
)
def resume_access_surface(access_surface_id: str, authorized: ManageProject, service: Service):
    _project_surface(access_surface_id, authorized, service)
    return project_ops.resume_connector(
        connector_id=access_surface_id, authorized=authorized, service=service
    )


@project_router.delete(
    "/{access_surface_id}", response_model=ApiResponse[None], dependencies=[query_contract()]
)
def delete_project_access_surface(
    access_surface_id: str, authorized: ManageProject, service: Service
):
    _project_surface(access_surface_id, authorized, service)
    return project_ops.delete_connector(
        connector_id=access_surface_id, authorized=authorized, service=service
    )
