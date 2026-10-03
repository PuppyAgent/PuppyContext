"""Canonical GitHub resources over the single existing application service."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.common_schemas import ApiResponse
from src.platform.query_contract import strict_query
from src.platform.synchronize.github import router as operations
from src.platform.synchronize.github import schemas as legacy
from src.platform.synchronize.github.public_schemas import (
    SynchronizeGithubBinding,
    SynchronizeGithubBindingCreate,
    SynchronizeGithubBindingUpdate,
    SynchronizeGithubBranches,
    SynchronizeGithubLogs,
    SynchronizeGithubPull,
    SynchronizeGithubPush,
    SynchronizeGithubRepos,
    SynchronizeGithubResult,
)
from src.platform.synchronize.github.service import GithubSyncNotFound

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/synchronize/github", tags=["synchronize-github"]
)
webhook_router = APIRouter(prefix="/api/v1/synchronize/github", tags=["synchronize-github"])


def _fields(value):
    return value.model_dump() if hasattr(value, "model_dump") else dict(value)


def _binding(value):
    fields = _fields(value)
    for old, new in (
        ("auto_import", "auto_pull"),
        ("last_imported_sha", "last_pulled_sha"),
        ("last_imported_at", "last_pulled_at"),
        ("last_exported_sha", "last_pushed_sha"),
        ("last_exported_at", "last_pushed_at"),
    ):
        fields[new] = fields.pop(old)
    return SynchronizeGithubBinding(**fields)


def _direction(value):
    return {"import": "inbound", "export": "outbound"}[value]


def _logs(value):
    fields = _fields(value)
    binding_id = fields.pop("integration_id")
    for row in fields["entries"]:
        if row.pop("integration_id") != binding_id:
            raise ValueError("GitHub log references another binding")
        row["synchronize_github_binding_id"] = binding_id
        row["direction"] = _direction(row["direction"])
    return SynchronizeGithubLogs(synchronize_github_binding_id=binding_id, **fields)


async def _reply(awaitable, project=lambda value: value):
    try:
        response = await awaitable
    except HTTPException as exc:
        if exc.status_code == 404 and exc.detail in (
            operations._DETAIL_NOT_CONFIGURED,
            operations._DETAIL_NOT_FOUND,
        ):
            raise HTTPException(
                404,
                {
                    "code": "SYNCHRONIZE_GITHUB_BINDING_NOT_FOUND",
                    "message": "GitHub Synchronize binding not found",
                },
            ) from exc
        if exc.status_code == 400 and isinstance(exc.detail, str):
            raise HTTPException(
                400, exc.detail.replace("auto_import requires", "auto_pull requires")
            ) from exc
        raise
    return ApiResponse(
        code=response.code,
        message="GitHub Synchronize operation completed",
        data=project(response.data) if response.data is not None else None,
    )


@router.post(
    "/binding", response_model=ApiResponse[SynchronizeGithubBinding], dependencies=[strict_query()]
)
async def create_binding(
    project_id: str, body: SynchronizeGithubBindingCreate, user=Depends(operations._manage_user)
):
    payload = legacy.GithubIntegrationCreate(
        auto_import=body.auto_pull, **body.model_dump(exclude={"auto_pull"})
    )
    return await _reply(operations.connect(project_id, payload, user), _binding)


@router.patch(
    "/binding", response_model=ApiResponse[SynchronizeGithubBinding], dependencies=[strict_query()]
)
async def update_binding(
    project_id: str, body: SynchronizeGithubBindingUpdate, user=Depends(operations._manage_user)
):
    fields = body.model_dump(exclude_unset=True)
    if "auto_pull" in fields:
        fields["auto_import"] = fields.pop("auto_pull")
    return await _reply(
        operations.update(project_id, legacy.GithubIntegrationUpdate(**fields), user), _binding
    )


@router.get(
    "/binding",
    response_model=ApiResponse[SynchronizeGithubBinding | None],
    dependencies=[strict_query()],
)
async def get_binding(project_id: str, user=Depends(operations._read_user)):
    return await _reply(operations.get_status(project_id, user), _binding)


@router.delete("/binding", response_model=ApiResponse[dict], dependencies=[strict_query()])
async def delete_binding(project_id: str, user=Depends(operations._manage_user)):
    return await _reply(operations.disconnect(project_id, user))


@router.get(
    "/repos",
    response_model=ApiResponse[SynchronizeGithubRepos],
    dependencies=[strict_query("oauth_connection_id")],
)
async def list_repos(
    project_id: str, oauth_connection_id: int = Query(gt=0), user=Depends(operations._read_user)
):
    return await _reply(
        operations.list_repos(project_id, oauth_connection_id, user),
        lambda value: SynchronizeGithubRepos(**_fields(value)),
    )


@router.get(
    "/branches",
    response_model=ApiResponse[SynchronizeGithubBranches],
    dependencies=[strict_query("oauth_connection_id", "repo_owner", "repo_name")],
)
async def list_branches(
    project_id: str,
    oauth_connection_id: int = Query(gt=0),
    repo_owner: str = Query(min_length=1),
    repo_name: str = Query(min_length=1),
    user=Depends(operations._read_user),
):
    return await _reply(
        operations.list_branches(project_id, oauth_connection_id, repo_owner, repo_name, user),
        lambda value: SynchronizeGithubBranches(**_fields(value)),
    )


async def _execute(project_id, payload, method):
    try:
        binding_id, result = await getattr(operations._service(), method)(project_id, payload)
    except GithubSyncNotFound as exc:
        raise HTTPException(
            404,
            {
                "code": "SYNCHRONIZE_GITHUB_BINDING_NOT_FOUND",
                "message": "GitHub Synchronize binding not found",
            },
        ) from exc
    fields = _fields(result)
    fields["direction"] = _direction(fields["direction"])
    return ApiResponse.success(
        data=SynchronizeGithubResult(synchronize_github_binding_id=binding_id, **fields)
    )


@router.post(
    "/pull", response_model=ApiResponse[SynchronizeGithubResult], dependencies=[strict_query()]
)
async def pull(project_id: str, body: SynchronizeGithubPull, user=Depends(operations._manage_user)):
    return await _execute(project_id, legacy.GithubImportRequest(**body.model_dump()), "pull")


@router.post(
    "/push", response_model=ApiResponse[SynchronizeGithubResult], dependencies=[strict_query()]
)
async def push(project_id: str, body: SynchronizeGithubPush, user=Depends(operations._manage_user)):
    return await _execute(project_id, legacy.GithubExportRequest(**body.model_dump()), "push")


@router.get(
    "/logs",
    response_model=ApiResponse[SynchronizeGithubLogs],
    dependencies=[strict_query("limit", "offset")],
)
async def list_logs(
    project_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(operations._read_user),
):
    return await _reply(operations.sync_log(project_id, limit, offset, user), _logs)


@webhook_router.post("/webhook", dependencies=[strict_query()])
async def webhook(request: Request):
    # Preserve raw bytes, authentication, replay checks and acknowledgement semantics.
    result = await operations.github_webhook(request)
    result = dict(result)
    if result.get("status") == "no_integration":
        result["status"] = "no_binding"
    if "results" in result:
        rows = []
        for old in result["results"]:
            row = dict(old)
            row["synchronize_github_binding_id"] = row.pop("integration_id")
            if row.get("reason") == "auto_import_disabled":
                row["reason"] = "auto_pull_disabled"
            elif row.get("reason") == "already_imported":
                row["reason"] = "already_pulled"
            rows.append(row)
        result["results"] = rows
    return result
