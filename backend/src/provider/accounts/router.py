"""Gateway API router — CRUD + OAuth for third-party account bindings.

Gateways represent third-party account connections (OAuth tokens, DB credentials)
that live at the org level and can be reused across multiple projects/access points.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.common_schemas import ApiResponse
from src.provider.accounts.schemas import (
    GatewayCreate,
    GatewayDetail,
    GatewayOut,
    GatewayUpdate,
)
from src.provider.accounts.service import GatewayService
from src.exceptions import ErrorCode, PermissionException
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.organization.dependencies import get_org_repository, resolve_org_id

router = APIRouter(prefix="/gateways", tags=["gateways"])


def _get_service() -> GatewayService:
    return GatewayService()


def _require_gateway_org_access(svc: GatewayService, gateway_id: str, user_id: str) -> dict:
    """Load a gateway and enforce that ``user_id`` is a member of its org.

    Gateways are org-scoped credential bindings. Without this check, any
    authenticated user could read/modify/delete/refresh any org's gateway by id.
    """
    gw = svc.get_by_id(gateway_id)
    if not get_org_repository().get_member(gw["org_id"], user_id):
        raise PermissionException(
            "Not a member of this gateway's organization", code=ErrorCode.FORBIDDEN
        )
    return gw


# ── List ───────────────────────────────────────────────────

@router.get(
    "/",
    response_model=ApiResponse[list[GatewayOut]],
    summary="List gateways for the current organization",
)
def list_gateways(
    provider: str | None = Query(None, description="Filter by provider"),
    org_id: str | None = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    resolved_org = resolve_org_id(org_id, current_user.user_id)
    gateways = svc.list_by_org(resolved_org, provider=provider)
    return ApiResponse.success(data=gateways, message="Gateways listed")


# ── Create (manual — for database, custom) ─────────────────
# NOTE: /{gateway_id} detail route is at the bottom of this file
# to avoid capturing fixed paths like /providers, /{provider}/authorize

@router.post(
    "/",
    response_model=ApiResponse[GatewayOut],
    summary="Create a gateway (manual credential entry)",
    status_code=status.HTTP_201_CREATED,
)
def create_gateway(
    payload: GatewayCreate,
    org_id: str | None = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    resolved_org = resolve_org_id(org_id, current_user.user_id)
    row = svc.create(
        org_id=resolved_org,
        user_id=current_user.user_id,
        provider=payload.provider,
        name=payload.name,
        credentials=payload.credentials,
        metadata=payload.metadata,
    )
    return ApiResponse.success(
        data=GatewayService._to_out(row),
        message="Gateway created",
    )


# ── Update ─────────────────────────────────────────────────

@router.patch(
    "/{gateway_id}",
    response_model=ApiResponse[GatewayOut],
    summary="Update gateway name/metadata/status",
)
def update_gateway(
    gateway_id: str,
    payload: GatewayUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    _require_gateway_org_access(svc, gateway_id, current_user.user_id)
    row = svc.update(
        gateway_id,
        name=payload.name,
        metadata=payload.metadata,
        status=payload.status,
    )
    return ApiResponse.success(
        data=GatewayService._to_out(row),
        message="Gateway updated",
    )


# ── Delete ─────────────────────────────────────────────────

@router.delete(
    "/{gateway_id}",
    response_model=ApiResponse,
    summary="Delete a gateway (must have no linked access points)",
)
def delete_gateway(
    gateway_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    _require_gateway_org_access(svc, gateway_id, current_user.user_id)
    svc.delete(gateway_id)
    return ApiResponse.success(message="Gateway deleted")


# ── Refresh token ──────────────────────────────────────────

@router.post(
    "/{gateway_id}/refresh-token",
    response_model=ApiResponse[GatewayOut],
    summary="Refresh OAuth token for a gateway",
)
async def refresh_token(
    gateway_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    _require_gateway_org_access(svc, gateway_id, current_user.user_id)
    row = await svc.refresh_token(gateway_id)
    return ApiResponse.success(
        data=GatewayService._to_out(row),
        message="Token refreshed",
    )


# ── Providers list ─────────────────────────────────────────

GATEWAY_PROVIDERS = [
    {"provider": "gmail", "display_name": "Gmail", "auth": "oauth"},
    {"provider": "github", "display_name": "GitHub", "auth": "oauth"},
    {"provider": "notion", "display_name": "Notion", "auth": "oauth"},
    {"provider": "google_drive", "display_name": "Google Drive", "auth": "oauth"},
    {"provider": "google_docs", "display_name": "Google Docs", "auth": "oauth"},
    {"provider": "google_sheets", "display_name": "Google Sheets", "auth": "oauth"},
    {"provider": "google_calendar", "display_name": "Google Calendar", "auth": "oauth"},
    {"provider": "google_search_console", "display_name": "Google Search Console", "auth": "oauth"},
    {"provider": "linear", "display_name": "Linear", "auth": "oauth"},
    {"provider": "airtable", "display_name": "Airtable", "auth": "oauth"},
    {"provider": "database", "display_name": "Database", "auth": "credentials"},
]


@router.get(
    "/providers",
    response_model=ApiResponse[list[dict]],
    summary="List available gateway providers",
)
def list_providers(
    current_user: CurrentUser = Depends(get_current_user),
):
    return ApiResponse.success(data=GATEWAY_PROVIDERS)


# ── OAuth authorize/callback ───────────────────────────────
# These delegate to the existing OAuth machinery but write to
# gateways table instead of oauth_connections.

@router.get(
    "/{provider}/authorize",
    summary="Get OAuth authorization URL for a provider",
)
def oauth_authorize(
    provider: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    # Delegate to existing OAuth service
    from src.provider.oauth.router import _get_oauth_service
    try:
        oauth_svc = _get_oauth_service(provider)
        auth_url = oauth_svc.get_authorize_url(current_user.user_id)
        return ApiResponse.success(data={"authorize_url": auth_url})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth not available for {provider}: {e}") from e


@router.post(
    "/{provider}/callback",
    response_model=ApiResponse[GatewayOut],
    summary="Handle OAuth callback and create gateway",
)
def oauth_callback(
    provider: str,
    body: dict,
    org_id: str | None = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    # Exchange code for tokens via existing OAuth service
    from src.provider.oauth.router import _get_oauth_service
    try:
        oauth_svc = _get_oauth_service(provider)
        token_data = oauth_svc.handle_callback(body.get("code", ""), current_user.user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth callback failed: {e}") from e

    resolved_org = resolve_org_id(org_id, current_user.user_id)

    # Create gateway with the OAuth credentials
    row = svc.create(
        org_id=resolved_org,
        user_id=current_user.user_id,
        provider=provider,
        name=token_data.get("workspace_name", provider),
        credentials={
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_type": token_data.get("token_type", "Bearer"),
            "expires_at": token_data.get("expires_at"),
        },
        metadata={
            "workspace_id": token_data.get("workspace_id"),
            "workspace_name": token_data.get("workspace_name"),
        },
    )
    return ApiResponse.success(
        data=GatewayService._to_out(row),
        message=f"{provider} gateway created",
    )


# ── Get detail (must be LAST — /{gateway_id} catches all) ──

@router.get(
    "/{gateway_id}",
    response_model=ApiResponse[GatewayDetail],
    summary="Get gateway details",
)
def get_gateway(
    gateway_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    svc: GatewayService = Depends(_get_service),
):
    _require_gateway_org_access(svc, gateway_id, current_user.user_id)
    detail = svc.get_detail(gateway_id)
    return ApiResponse.success(data=detail)
