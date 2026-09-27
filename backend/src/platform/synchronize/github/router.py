"""HTTP router for the GitHub Integration module.

Mounted at ``/api/v1/projects/{project_id}/github`` (per-project) and
``/api/v1/internal/github/webhook`` (callback from GitHub).

Endpoints:

* ``POST   /api/v1/projects/{pid}/github/connect``    — bind project ↔ repo
* ``PATCH  /api/v1/projects/{pid}/github``            — update binding
* ``DELETE /api/v1/projects/{pid}/github``            — disconnect
* ``GET    /api/v1/projects/{pid}/github/status``     — current binding
* ``GET    /api/v1/projects/{pid}/github/repos``      — repo picker
* ``POST   /api/v1/projects/{pid}/github/import``     — manual import
* ``POST   /api/v1/projects/{pid}/github/export``     — manual export
* ``GET    /api/v1/projects/{pid}/github/sync-log``   — recent imports/exports

Plus the public webhook receiver (no per-project prefix because GitHub
delivers to a single URL):

* ``POST   /api/v1/integrations/github/webhook``
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.synchronize.github.schemas import (
    GithubBranchList,
    GithubExportRequest, GithubImportRequest, GithubIntegrationCreate,
    GithubIntegrationStatus, GithubIntegrationUpdate,
    GithubRepoList, GithubSyncLogList, GithubSyncRunResult,
)
from src.platform.synchronize.github.service import (
    GithubSyncNotFound, GithubSyncService,
)
from src.platform.synchronize.github.webhook import WebhookRejection, handle_webhook
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


# Per-project router — gated by JWT.
router = APIRouter(
    prefix="/api/v1/projects/{project_id}/github",
    tags=["github-integration"],
)

# Public webhook receiver — no auth gate (HMAC verifies authenticity).
webhook_router = APIRouter(
    prefix="/api/v1/integrations/github",
    tags=["github-integration"],
)


def _service() -> GithubSyncService:
    return GithubSyncService()


# ── Project-scoped routes ──────────────────────────────


@router.post("/connect", response_model=ApiResponse[GithubIntegrationStatus])
async def connect(
    project_id: str,
    payload: GithubIntegrationCreate,
    user=Depends(get_current_user),
) -> ApiResponse[GithubIntegrationStatus]:
    try:
        result = await _service().connect(project_id, payload)
        return ApiResponse.success(data=result, message="github integration connected")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        log_error(f"[GithubIntegration] connect failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("", response_model=ApiResponse[GithubIntegrationStatus])
async def update(
    project_id: str,
    payload: GithubIntegrationUpdate,
    user=Depends(get_current_user),
) -> ApiResponse[GithubIntegrationStatus]:
    try:
        result = await _service().update(project_id, payload)
        return ApiResponse.success(data=result, message="github integration updated")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("", response_model=ApiResponse[dict])
async def disconnect(
    project_id: str,
    user=Depends(get_current_user),
) -> ApiResponse[dict]:
    await _service().disconnect(project_id)
    return ApiResponse.success(data={}, message="github integration disconnected")


@router.get("/status", response_model=ApiResponse[GithubIntegrationStatus | None])
async def get_status(
    project_id: str,
    user=Depends(get_current_user),
) -> ApiResponse[GithubIntegrationStatus | None]:
    result = await _service().status(project_id)
    return ApiResponse.success(data=result, message="github integration status retrieved")


@router.get("/repos", response_model=ApiResponse[GithubRepoList])
async def list_repos(
    project_id: str,
    oauth_connection_id: int = Query(..., description="The user's GitHub OAuth row id"),
    user=Depends(get_current_user),
) -> ApiResponse[GithubRepoList]:
    """List the OAuth user's GitHub repositories.

    The OAuth connection id is supplied explicitly so the UI can drive
    the picker before the integration is bound (we don't yet know which
    OAuth row to use). After ``connect`` the same id is stored on the
    integration row and reused for imports/exports.
    """
    try:
        result = await _service().list_user_repos(oauth_connection_id)
        return ApiResponse.success(data=result, message="repositories retrieved")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_OAUTH_NOT_FOUND)
    except Exception as e:  # noqa: BLE001
        log_error(f"[GithubIntegration] list_repos failed: {e}")
        raise HTTPException(status_code=502, detail=f"GitHub API: {e}")


@router.get("/branches", response_model=ApiResponse[GithubBranchList])
async def list_branches(
    project_id: str,
    oauth_connection_id: int = Query(..., description="The user's GitHub OAuth row id"),
    repo_owner: str = Query(..., min_length=1),
    repo_name: str = Query(..., min_length=1),
    user=Depends(get_current_user),
) -> ApiResponse[GithubBranchList]:
    """List branches for a (owner, repo) pair.

    Drives the connect-form's branch dropdown. ``oauth_connection_id``
    is the same query param shape as ``/repos`` so the frontend can
    pass the OAuth id it already has cached.
    """
    try:
        result = await _service().list_repo_branches(
            oauth_connection_id, repo_owner, repo_name,
        )
        return ApiResponse.success(data=result, message="branches retrieved")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_OAUTH_NOT_FOUND)
    except Exception as e:  # noqa: BLE001
        log_error(f"[GithubIntegration] list_branches failed: {e}")
        raise HTTPException(status_code=502, detail=f"GitHub API: {e}")


@router.post("/import", response_model=ApiResponse[GithubSyncRunResult])
async def import_now(
    project_id: str,
    payload: GithubImportRequest,
    user=Depends(get_current_user),
) -> ApiResponse[GithubSyncRunResult]:
    try:
        result = await _service().import_now(project_id, payload)
        return ApiResponse.success(data=result, message="github import completed")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_CONFIGURED)


@router.post("/export", response_model=ApiResponse[GithubSyncRunResult])
async def export_now(
    project_id: str,
    payload: GithubExportRequest,
    user=Depends(get_current_user),
) -> ApiResponse[GithubSyncRunResult]:
    try:
        result = await _service().export_now(project_id, payload)
        return ApiResponse.success(data=result, message="github export completed")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_CONFIGURED)


@router.get("/sync-log", response_model=ApiResponse[GithubSyncLogList])
async def sync_log(
    project_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
) -> ApiResponse[GithubSyncLogList]:
    try:
        result = await _service().list_sync_log(
            project_id, limit=limit, offset=offset,
        )
        return ApiResponse.success(data=result, message="sync log retrieved")
    except GithubSyncNotFound:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_CONFIGURED)


# ── Webhook receiver ───────────────────────────────────


@webhook_router.post("/webhook")
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
    except Exception as e:  # noqa: BLE001
        log_error(f"[GithubWebhook] unexpected: {e}")
        raise HTTPException(status_code=500, detail="webhook handler failed")
