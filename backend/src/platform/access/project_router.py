"""Project Access operations; public_router owns the resource HTTP boundary."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query

from src.common_schemas import ApiResponse
from src.exceptions import AppException
from src.platform.access.models import AccessSurface
from src.platform.access.router import _contains_secret_config_key, _redact_config
from src.platform.access.service import AccessService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import AuthorizedProject, require_project_action
from src.platform.authorization.models import ProjectAction
from src.platform.repository_target.schemas import (
    repository_target_domain,
    repository_target_schema,
)
from src.repo.schemas import (
    ConnectorIn,
    ConnectorOut,
    ConnectorPatch,
    TargetAccessEnableIn,
)

def get_access_service() -> AccessService:
    return AccessService()


def _reject_credential_metadata(*values) -> None:
    if any(_contains_secret_config_key(value) for value in values):
        raise HTTPException(400, "Credentials cannot be written through Access metadata; use explicit credential issuance.")


def _to_out(c: AccessSurface) -> ConnectorOut:
    return ConnectorOut(
        id=c.id,
        target=repository_target_schema(c.target),
        provider=c.kind,
        name=c.name,
        direction=c.direction,                    # type: ignore[arg-type]
        config=_redact_config(c.config),
        policy=_redact_config(c.policy),
        oauth_connection_id=c.oauth_connection_id,
        trigger=_redact_config(c.trigger),
        status=c.status,
        last_run_at=c.last_run_at,
        last_run_id=c.last_run_id,
        error_message=c.error_message,
        created_by=c.created_by,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def list_connectors(
    provider: str | None = Query(None),
    direction: str | None = Query(None),
    include_non_access: bool = Query(
        False,
        description=(
            "Include legacy import-only connector rows. The default response "
            "contains only ongoing Access methods."
        ),
    ),
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_READ)
    ),
    service: AccessService = Depends(get_access_service),
):
    items = service.list(
        str(authorized.project.id),
        kind=provider,
        direction=direction,
        access_surface_only=not include_non_access,
    )
    return ApiResponse.success(data=[_to_out(c) for c in items], message="Connectors listed")


def create_connector(
    payload: ConnectorIn,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_MANAGE)
    ),
    current_user: CurrentUser = Depends(get_current_user),
    service: AccessService = Depends(get_access_service),
):
    _reject_credential_metadata(payload.config, payload.policy, payload.trigger.model_dump() if payload.trigger else None)
    try:
        c = service.create(
            project_id=str(authorized.project.id),
            target=repository_target_domain(payload.target),
            kind=payload.provider,
            direction=payload.direction,
            name=payload.name,
            config=payload.config,
            policy=payload.policy,
            oauth_connection_id=payload.oauth_connection_id,
            trigger=(payload.trigger.model_dump() if payload.trigger else None),
            created_by=current_user.user_id,
        )
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    return ApiResponse.success(data=_to_out(c), message="Connector created")


def enable_target_access(
    payload: TargetAccessEnableIn,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_MANAGE)
    ),
    current_user: CurrentUser = Depends(get_current_user),
    service: AccessService = Depends(get_access_service),
):
    try:
        connectors = service.enable_target_defaults(
            project_id=str(authorized.project.id),
            target=repository_target_domain(payload.target),
            created_by=current_user.user_id,
        )
    except AppException as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.message,
        ) from error
    return ApiResponse.success(
        data=[_to_out(connector) for connector in connectors],
        message="Repository target access enabled",
    )


def update_connector(
    connector_id: str,
    payload: ConnectorPatch,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_MANAGE)
    ),
    service: AccessService = Depends(get_access_service),
):
    existing = service.get(connector_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Connector not found")
    _reject_credential_metadata(payload.config, payload.policy, payload.trigger.model_dump() if payload.trigger else None)
    patch = payload.model_dump(exclude_unset=True)
    if "trigger" in patch and patch["trigger"] is not None:
        # Pydantic gave us a TriggerSpec dict-like; pass through.
        patch["trigger"] = dict(patch["trigger"])
    try:
        updated = service.update(connector_id, patch)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    if updated is None:
        raise HTTPException(status_code=404, detail="Connector not found after update")
    return ApiResponse.success(data=_to_out(updated), message="Connector updated")


def activate_agent_connector(
    connector_id: str,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.AGENT_MANAGE)
    ),
    service: AccessService = Depends(get_access_service),
):
    existing = service.get(connector_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Connector not found")
    try:
        updated = service.activate_agent_surface(connector_id)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    if updated is None:
        raise HTTPException(status_code=404, detail="Connector not found after activation")
    return ApiResponse.success(data=_to_out(updated), message="Agent connector activated")


async def run_connector(
    connector_id: str,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.AUTOMATION_RUN)
    ),
    service: AccessService = Depends(get_access_service),
):
    existing = service.get(connector_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Connector not found")
    try:
        run_id = await service.run_now(connector_id)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    return ApiResponse.success(data={"run_id": run_id}, message="Run triggered")


def pause_connector(
    connector_id: str,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_MANAGE)
    ),
    service: AccessService = Depends(get_access_service),
):
    existing = service.get(connector_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Connector not found")
    service.pause(connector_id)
    return ApiResponse.success(message="Connector paused")


def resume_connector(
    connector_id: str,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_MANAGE)
    ),
    service: AccessService = Depends(get_access_service),
):
    existing = service.get(connector_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Connector not found")
    service.resume(connector_id)
    return ApiResponse.success(message="Connector resumed")


def delete_connector(
    connector_id: str,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_MANAGE)
    ),
    service: AccessService = Depends(get_access_service),
):
    existing = service.get(connector_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Connector not found")
    try:
        service.delete(connector_id)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    return ApiResponse.success(message="Connector deleted")
