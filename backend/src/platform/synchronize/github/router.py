"""GitHub Synchronize operations; public_router alone declares resource HTTP routes."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.synchronize.github.schemas import (
    GithubBranchList,
    GithubExportRequest,
    GithubImportRequest,
    GithubIntegrationCreate,
    GithubIntegrationStatus,
    GithubIntegrationUpdate,
    GithubRepoList,
    GithubSyncLogList,
    GithubSyncRunResult,
)
from src.platform.synchronize.github.service import (
    GithubSyncNotFound,
    GithubSyncService,
)
from src.platform.synchronize.github.webhook import WebhookRejection, handle_webhook
from src.provider.oauth.repository import OAuthRepository
from src.utils.logger import log_error, log_info

# All authenticated endpoints below return ``ApiResponse[T]`` rather than
# the raw payload — the frontend's ``apiClient.apiRequest`` reads
# ``data.code !== 0`` to decide success/failure and falls back to the
# string "API request failed" when the envelope is missing. The webhook
# receiver is the lone exception (raw 200 ack expected by GitHub).

# Shared 404 detail strings — kept as module-level constants so the same
# wording is used everywhere a binding lookup misses (frontend matches
# on this exact text in some flows; changing one site without the others
# would silently break those checks).
_DETAIL_NOT_CONFIGURED = "github integration not configured"
_DETAIL_NOT_FOUND = "github integration not found"
_DETAIL_OAUTH_NOT_FOUND = "oauth connection not found"


def _service() -> GithubSyncService:
    return GithubSyncService()


def _project_user(action: ProjectAction):
    def authorize(
        project_id: str,
        user: CurrentUser = Depends(get_current_user),
        authorization: AuthorizationService = Depends(get_authorization_service),
    ):
        authorization.authorize(project_id, user.user_id, action)
        return user
    return authorize


_read_user = _project_user(ProjectAction.ACCESS_READ)
_manage_user = _project_user(ProjectAction.SYNCHRONIZE_MANAGE)


async def _require_owned_github_oauth(oauth_connection_id: int, user: CurrentUser):
    oauth = await OAuthRepository().get_by_id(oauth_connection_id)
    if oauth is None or oauth.user_id != user.user_id or oauth.provider != "github":
        raise HTTPException(404, detail=_DETAIL_OAUTH_NOT_FOUND)


# ── Project-scoped routes ──────────────────────────────


async def connect(
    project_id: str,
    payload: GithubIntegrationCreate,
    user=Depends(_manage_user),
) -> ApiResponse[GithubIntegrationStatus]:
    await _require_owned_github_oauth(payload.oauth_connection_id, user)
    try:
        result = await _service().connect(project_id, payload)
        return ApiResponse.success(data=result, message="github integration connected")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        log_error("[GithubIntegration] binding setup failed")
        raise HTTPException(status_code=500, detail="GitHub binding setup failed")


async def update(
    project_id: str,
    payload: GithubIntegrationUpdate,
    user=Depends(_manage_user),
) -> ApiResponse[GithubIntegrationStatus]:
    try:
        result = await _service().update(project_id, payload)
        return ApiResponse.success(data=result, message="github integration updated")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def disconnect(
    project_id: str,
    user=Depends(_manage_user),
) -> ApiResponse[dict]:
    await _service().disconnect(project_id)
    return ApiResponse.success(data={}, message="github integration disconnected")


async def get_status(
    project_id: str,
    user=Depends(_read_user),
) -> ApiResponse[GithubIntegrationStatus | None]:
    result = await _service().status(project_id)
    return ApiResponse.success(data=result, message="github integration status retrieved")


async def list_repos(
    project_id: str,
    oauth_connection_id: int = Query(..., description="The user's GitHub OAuth row id"),
    user=Depends(_read_user),
) -> ApiResponse[GithubRepoList]:
    """List the OAuth user's GitHub repositories.

    The OAuth connection id is supplied explicitly so the UI can drive
    the picker before the integration is bound (we don't yet know which
    OAuth row to use). After ``connect`` the same id is stored on the
    integration row and reused for imports/exports.
    """
    await _require_owned_github_oauth(oauth_connection_id, user)
    try:
        result = await _service().list_user_repos(oauth_connection_id)
        return ApiResponse.success(data=result, message="repositories retrieved")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_OAUTH_NOT_FOUND)
    except Exception as e:
        log_error("[GithubIntegration] repository discovery failed")
        raise HTTPException(status_code=502, detail="GitHub repository discovery failed") from e


async def list_branches(
    project_id: str,
    oauth_connection_id: int = Query(..., description="The user's GitHub OAuth row id"),
    repo_owner: str = Query(..., min_length=1),
    repo_name: str = Query(..., min_length=1),
    user=Depends(_read_user),
) -> ApiResponse[GithubBranchList]:
    """List branches for a (owner, repo) pair.

    Drives the connect-form's branch dropdown. ``oauth_connection_id``
    is the same query param shape as ``/repos`` so the frontend can
    pass the OAuth id it already has cached.
    """
    await _require_owned_github_oauth(oauth_connection_id, user)
    try:
        result = await _service().list_repo_branches(
            oauth_connection_id, repo_owner, repo_name,
        )
        return ApiResponse.success(data=result, message="branches retrieved")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_OAUTH_NOT_FOUND)
    except Exception as e:
        log_error("[GithubIntegration] branch discovery failed")
        raise HTTPException(status_code=502, detail="GitHub branch discovery failed") from e


async def import_now(
    project_id: str,
    payload: GithubImportRequest,
    user=Depends(_manage_user),
) -> ApiResponse[GithubSyncRunResult]:
    try:
        result = await _service().import_now(project_id, payload)
        return ApiResponse.success(data=result, message="github import completed")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_CONFIGURED)


async def export_now(
    project_id: str,
    payload: GithubExportRequest,
    user=Depends(_manage_user),
) -> ApiResponse[GithubSyncRunResult]:
    try:
        result = await _service().export_now(project_id, payload)
        return ApiResponse.success(data=result, message="github export completed")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_CONFIGURED)


async def sync_log(
    project_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(_read_user),
) -> ApiResponse[GithubSyncLogList]:
    try:
        result = await _service().list_sync_log(
            project_id, limit=limit, offset=offset,
        )
        return ApiResponse.success(data=result, message="sync log retrieved")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_CONFIGURED)


# ── Webhook receiver ───────────────────────────────────


async def github_webhook(request: Request):
    raw = await request.body()
    try:
        json_payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    headers = {k.lower(): v for k, v in request.headers.items()}
    log_info(
        f"[GithubWebhook] received delivery="
        f"{headers.get('x-github-delivery', '?')}"
    )

    try:
        return await handle_webhook(raw, headers, json_payload)
    except WebhookRejection as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        log_error(f"[GithubWebhook] unexpected: {e}")
        raise HTTPException(status_code=500, detail="webhook handler failed")
