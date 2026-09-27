"""OAuth router — factory-generated endpoints for all providers.

Each provider gets 4 endpoints (authorize, callback, status, disconnect)
generated from a shared template. This replaces ~1200 lines of duplicated
code with a ~120-line factory + provider config table.
"""

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException

from src.config import settings
from src.platform.auth.dependencies import get_current_user
from src.common_schemas import ApiResponse
from src.provider.oauth.dependencies import (
    get_notion_service,
    get_github_service,
    get_google_sheets_service,
    get_gmail_service,
    get_google_drive_service,
    get_google_calendar_service,
    get_google_docs_service,
    get_google_search_console_service,
    get_linear_service,
    get_airtable_service,
)
from src.provider.oauth.schemas import (
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    OAuthCallbackResponse,
    OAuthStatusResponse,
    OAuthDisconnectResponse,
)
from src.provider.oauth.state_repository import OAuthStateRepository
from src.utils.logger import log_warning

router = APIRouter(prefix="/oauth", tags=["oauth"])


# ── Provider configuration ────────────────────────────────────

def _check_notion() -> bool:
    return bool(settings.NOTION_CLIENT_ID and settings.NOTION_CLIENT_SECRET)

def _check_github() -> bool:
    return bool(settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET)

def _check_google() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)

def _check_linear() -> bool:
    return bool(settings.LINEAR_CLIENT_ID and settings.LINEAR_CLIENT_SECRET)

def _check_airtable() -> bool:
    return bool(settings.AIRTABLE_CLIENT_ID and settings.AIRTABLE_CLIENT_SECRET)


OAUTH_PROVIDERS: list[dict] = [
    {"name": "Notion",          "slug": "notion",          "dep": get_notion_service,          "check": _check_notion},
    {"name": "GitHub",          "slug": "github",          "dep": get_github_service,          "check": _check_github},
    {"name": "Google Sheets",   "slug": "google-sheets",   "dep": get_google_sheets_service,   "check": _check_google},
    {"name": "Linear",          "slug": "linear",          "dep": get_linear_service,          "check": _check_linear},
    {"name": "Airtable",        "slug": "airtable",        "dep": get_airtable_service,        "check": _check_airtable},
    {"name": "Gmail",           "slug": "gmail",           "dep": get_gmail_service,           "check": _check_google},
    {"name": "Google Drive",    "slug": "google-drive",    "dep": get_google_drive_service,    "check": _check_google},
    {"name": "Google Calendar",  "slug": "google-calendar", "dep": get_google_calendar_service, "check": _check_google},
    {"name": "Google Docs",     "slug": "google-docs",     "dep": get_google_docs_service,     "check": _check_google},
    {"name": "Google Search Console", "slug": "google-search-console", "dep": get_google_search_console_service, "check": _check_google},
]


# ── Endpoint factory ──────────────────────────────────────────

def _register_oauth_provider(
    target_router: APIRouter,
    name: str,
    slug: str,
    dep: Callable,
    check: Callable[[], bool],
) -> None:
    """Register the 4 standard OAuth endpoints for a single provider."""

    @target_router.get(
        f"/{slug}/authorize",
        response_model=ApiResponse[OAuthAuthorizeResponse],
        name=f"{slug}_authorize",
    )
    async def authorize(
        current_user=Depends(get_current_user),
        service=Depends(dep),
    ):
        f"""Get {name} OAuth authorization URL."""
        try:
            if not check():
                raise HTTPException(
                    status_code=500,
                    detail=f"{name} OAuth is not configured. "
                           f"Please set the required environment variables.",
                )

            # SECURITY (M-2): issue a server-side state nonce bound to the
            # caller. The matching callback() must present this exact state,
            # otherwise a malicious site could trick the user into linking
            # the attacker's OAuth identity to the victim's account.
            state_repo = OAuthStateRepository()
            state = state_repo.issue(
                user_id=current_user.user_id, provider=slug,
            )
            authorization_url, _ = await service.get_authorization_url(state=state)
            return ApiResponse.success(
                data=OAuthAuthorizeResponse(authorization_url=authorization_url),
                message=f"{name} authorization URL generated",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate {name} authorization URL: {e}",
            )

    @target_router.post(
        f"/{slug}/callback",
        response_model=ApiResponse[OAuthCallbackResponse],
        name=f"{slug}_callback",
    )
    async def callback(
        request: OAuthCallbackRequest,
        service=Depends(dep),
    ):
        f"""Handle {name} OAuth callback."""
        try:
            # SECURITY (M-2): validate the state nonce server-side. The state
            # was issued only after an authenticated authorize request, so the
            # callback can recover the Puppyone user from it instead of
            # depending on the browser still having a fresh app JWT after the
            # third-party redirect. consume_for_provider() is single-use.
            state_repo = OAuthStateRepository()
            user_id = state_repo.consume_for_provider(
                state=request.state or "",
                provider=slug,
            )
            if not user_id:
                log_warning(
                    f"[OAuth] state validation failed provider={slug}"
                )
                raise HTTPException(
                    status_code=400,
                    detail="Invalid or expired OAuth state",
                )

            success, message, connection_info = await service.handle_callback(
                user_id=user_id, code=request.code,
            )
            return ApiResponse.success(
                data=OAuthCallbackResponse(
                    success=success,
                    message=message,
                    workspace_name=(
                        connection_info.get("workspace_name")
                        if connection_info else None
                    ),
                ),
                message=f"{name} OAuth callback handled",
            )
        except HTTPException:
            # Preserve intentional status codes (e.g. the 400 from the
            # state-validation block above) — without this re-raise, the
            # outer `except Exception` masked them as opaque 500s, making
            # CSRF mismatches look like server bugs to the client.
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to handle {name} callback: {e}",
            )

    @target_router.get(
        f"/{slug}/status",
        response_model=ApiResponse[OAuthStatusResponse],
        name=f"{slug}_status",
    )
    async def status(
        current_user=Depends(get_current_user),
        service=Depends(dep),
    ):
        f"""Check {name} access status."""
        try:
            connection = await service.get_connection(current_user.user_id)
            if connection:
                is_expired = await service.is_token_expired(current_user.user_id)
                if is_expired:
                    connection = await service.refresh_token_if_needed(
                        current_user.user_id
                    )
                return ApiResponse.success(
                    data=OAuthStatusResponse(
                        connected=connection is not None,
                        workspace_name=connection.workspace_name if connection else None,
                        connected_at=connection.created_at if connection else None,
                        connection_id=connection.id if connection else None,
                    ),
                    message=f"{name} access status retrieved",
                )
            return ApiResponse.success(
                data=OAuthStatusResponse(
                    connected=False, workspace_name=None, connected_at=None,
                    connection_id=None,
                ),
                message=f"{name} access status retrieved",
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to check {name} status: {e}",
            )

    @target_router.delete(
        f"/{slug}/disconnect",
        response_model=ApiResponse[OAuthDisconnectResponse],
        name=f"{slug}_disconnect",
    )
    async def disconnect(
        current_user=Depends(get_current_user),
        service=Depends(dep),
    ):
        f"""Disconnect {name} integration."""
        try:
            success = await service.disconnect(current_user.user_id)
            if success:
                return ApiResponse.success(
                    data=OAuthDisconnectResponse(
                        success=True,
                        message=f"Successfully disconnected from {name}",
                    ),
                    message=f"{name} disconnected",
                )
            return ApiResponse.success(
                data=OAuthDisconnectResponse(
                    success=False,
                    message=f"No active {name} access found",
                ),
                message=f"No active {name} access",
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to disconnect from {name}: {e}",
            )


# ── Register all providers ────────────────────────────────────

for _provider in OAUTH_PROVIDERS:
    _register_oauth_provider(
        router,
        name=_provider["name"],
        slug=_provider["slug"],
        dep=_provider["dep"],
        check=_provider["check"],
    )
