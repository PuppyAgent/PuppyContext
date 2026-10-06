"""Commit history API — commits, commit-content, diff, rollback.

All commit identity is hash-based (40-hex SHA-1 ``commit_id`` over the
Git ``commit`` object body). The default linear mode preserves the legacy
``(created_at ASC, commit_id ASC)`` catch-up contract. ``order=topo`` exposes
the all-ref Git DAG in deterministic child-before-parent order and pages older
commits with an exclusive cursor.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService
from src.platform.authorization.models import ProjectAction
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.admission.validation import validate_path
from src.version_engine.bootstrap.dependencies import (
    get_product_operation_adapter,
    get_history_graph_service,
    get_repo_manager,
    get_version_admin_service,
)
from src.version_engine.domain.errors import VersionEngineError
from src.version_engine.entrypoints.http.content_helpers import (
    ensure_project_access,
    ensure_write_access,
)
from src.version_engine.entrypoints.http.schemas import (
    DiffResponse,
    FileVersionInfo,
    RollbackRequest,
    RollbackResponse,
    VersionHistoryRef,
    VersionHistoryResponse,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.admin import VersionAdminService
from src.version_engine.read.history_graph import HistoryGraphService
from src.version_engine.read.history_changes import normalize_history_changes
from src.version_engine.read.history_models import (
    HistoryCursorError,
    HistoryGraphTooLargeError,
    HistoryRefsUnavailableError,
    HistorySnapshotUnavailableError,
)

history_router = APIRouter()


@history_router.get("/{project_id}/head", summary="Canonical project history head")
async def get_project_head(
    project_id: str,
    version_admin: VersionAdminService = Depends(get_version_admin_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    grant = await run_in_threadpool(authorization.authorize, project_id, current_user.user_id, ProjectAction.HISTORY_READ)
    from src.version_engine.entrypoints.http.repository_management import read_native_head
    from src.version_engine.infrastructure.owned_work import run_owned
    native = await run_owned(read_native_head, repo_manager, grant)
    if native is not None:
        return ApiResponse.success(data=native)
    head = await version_admin.get_project_head_commit_id(project_id)
    return ApiResponse.success(data={"project_id": project_id, "head_commit_id": head})


@history_router.get(
    "/{project_id}/commits",
    response_model=ApiResponse[VersionHistoryResponse],
    summary="Commit history",
)
async def get_commits(
    project_id: str,
    path: str = Query(None, description="File path (omit for project-level history)"),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of results"),
    since_commit_id: str = Query(
        "",
        description=(
            "Exclusive anchor — only commits strictly newer than this one "
            "are returned. Leave empty to fetch the most recent ``limit`` "
            "commits (the default)."
        ),
    ),
    cursor: str = Query(
        "",
        description="Exclusive cursor for loading older topological-history pages.",
    ),
    order: Literal["linear", "topo"] = Query(
        "linear",
        description=(
            "linear preserves the legacy transaction catch-up contract; topo "
            "returns all commits reachable from the project's branch/tag refs."
        ),
    ),
    version_admin: VersionAdminService = Depends(get_version_admin_service),
    history_graph: HistoryGraphService = Depends(get_history_graph_service),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    await run_in_threadpool(ensure_project_access, authorization, current_user, project_id)

    if cursor and since_commit_id:
        raise HTTPException(
            status_code=400,
            detail="cursor and since_commit_id are mutually exclusive",
        )
    graph_mode = order == "topo" or bool(cursor)
    if graph_mode and (path or since_commit_id):
        raise HTTPException(
            status_code=400,
            detail="topological history is project-level and uses cursor pagination",
        )

    refs: list[VersionHistoryRef] = []
    refs_included = False
    snapshot_id = ""
    next_cursor: str | None = None
    has_more = False
    graph_health: Literal["complete", "degraded"] = "complete"
    unreadable_commit_ids: list[str] = []
    if graph_mode:
        try:
            page = await history_graph.get_page(
                project_id,
                limit=limit,
                cursor=cursor,
            )
        except HistoryCursorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HistorySnapshotUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HistoryRefsUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except HistoryGraphTooLargeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        entries = page.entries
        total = page.total
        next_cursor = page.next_cursor
        has_more = page.has_more
        refs = [
            VersionHistoryRef(
                ref_name=ref.ref_name,
                ref_type=ref.ref_type,
                commit_id=ref.commit_id,
            )
            for ref in page.refs
        ]
        refs_included = page.refs_included
        head_commit_id = page.head_commit_id
        snapshot_id = page.snapshot_id
        graph_health = page.graph_health
        unreadable_commit_ids = list(page.unreadable_commit_ids)
    else:
        entries = await version_admin.get_commit_history(
            project_id=project_id,
            path=validate_path(path) if path else None,
            limit=limit,
            since_commit_id=since_commit_id,
        )
        parents_by_commit = await version_admin.get_commit_parent_ids(
            project_id,
            [entry.get("commit_id", "") for entry in entries],
        )
        entries = [
            {
                **entry,
                "parent_ids": parents_by_commit.get(entry.get("commit_id", ""), []),
            }
            for entry in entries
        ]
        total = len(entries)
        head_commit_id = await version_admin.get_project_head_commit_id(project_id)

    commits = [
        FileVersionInfo(
            commit_id=e.get("commit_id", ""),
            parent_ids=e.get("parent_ids") or [],
            who=e.get("who", ""),
            message=e.get("message", ""),
            changes=normalize_history_changes(e.get("changes")),
            conflicts=e.get("conflicts") or [],
            root_hash=e.get("root_hash", ""),
            scope_hash=e.get("scope_hash", ""),
            scope_path=e.get("scope_path", ""),
            created_at=e.get("created_at"),
            audit_detail=e.get("audit_detail"),
        )
        for e in entries
    ]
    if not head_commit_id and commits:
        head_commit_id = commits[0].commit_id if graph_mode else commits[-1].commit_id

    root_hash = await run_in_threadpool(ops.get_root_hash, project_id) or ""

    return ApiResponse.success(data=VersionHistoryResponse(
        project_id=project_id,
        path=path,
        head_commit_id=head_commit_id,
        root_hash=root_hash,
        commits=commits,
        refs=refs,
        refs_included=refs_included,
        snapshot_id=snapshot_id,
        next_cursor=next_cursor,
        has_more=has_more,
        graph_health=graph_health,
        unreadable_commit_ids=unreadable_commit_ids,
        total=total,
    ))


@history_router.get(
    "/{project_id}/commit-content",
    summary="Get file contents at a specific commit",
)
async def get_commit_content(
    project_id: str,
    path: str = Query(..., description="File path"),
    commit_id: str = Query(..., description="Commit id (40-hex SHA-1)"),
    preview_bytes: Annotated[int | None, Query(ge=1, le=1_048_576)] = None,
    version_admin: VersionAdminService = Depends(get_version_admin_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    await run_in_threadpool(ensure_project_access, authorization, current_user, project_id)

    clean_path = validate_path(path)
    try:
        content = await version_admin.get_commit_content(project_id, clean_path, commit_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))

    return await run_in_threadpool(_format_commit_content, clean_path, commit_id, content, preview_bytes)


def _format_commit_content(clean_path: str, commit_id: str, content: bytes, preview_bytes: int | None):

    from src.version_engine.read.tree_reader import detect_mime, detect_type
    from src.version_engine.read.text_detection import is_binary_content

    node_type = detect_type(clean_path)
    mime_type = detect_mime(clean_path)
    base = {
        "path": clean_path,
        "commit_id": commit_id,
        "type": node_type,
        "mime_type": mime_type,
        "size_bytes": len(content),
    }

    # Preview reads are additive. Full-content clients retain their contract;
    # a large preview must not transfer/parse an entire JSON document in the UI.
    if preview_bytes is not None and len(content) > preview_bytes:
        return ApiResponse.success(data={**base, "truncated": True})

    if node_type == "json":
        try:
            return ApiResponse.success(data={
                **base,
                "is_binary": False,
                "content": _json.loads(content.decode("utf-8")),
            })
        except ValueError:
            pass

    if is_binary_content(content, node_type=node_type, mime_type=mime_type):
        return ApiResponse.success(data={**base, "is_binary": True})

    return ApiResponse.success(data={
        **base,
        "is_binary": False,
        "content_text": content.decode("utf-8", errors="replace"),
    })


@history_router.get(
    "/{project_id}/diff",
    response_model=ApiResponse[DiffResponse],
    summary="Compare two commits",
)
async def diff_commits(
    project_id: str,
    from_commit_id: str = Query(..., description="Source commit id"),
    to_commit_id: str = Query(..., description="Target commit id"),
    version_admin: VersionAdminService = Depends(get_version_admin_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    await run_in_threadpool(ensure_project_access, authorization, current_user, project_id)

    try:
        changes = await version_admin.compute_diff(project_id, from_commit_id, to_commit_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except VersionEngineError as e:
        # ObjectNotFoundError (unreadable commit tree) and friends carry their
        # own http_status; surface that instead of an unhandled 500.
        raise HTTPException(status_code=getattr(e, "http_status", 500), detail=str(e))

    # compute_diff/diff_trees emit {"path", "op"}; DiffResponse.DiffItem
    # requires "change_type". Map op→change_type so the response validates
    # (the raw shape was the actual source of the diff 500 — pydantic
    # ValidationError on the missing change_type field).
    return ApiResponse.success(data=DiffResponse(
        project_id=project_id,
        from_commit_id=from_commit_id,
        to_commit_id=to_commit_id,
        changes=[
            {"path": c["path"], "change_type": c.get("op", "modified")}
            for c in changes
        ],
    ))


@history_router.post(
    "/{project_id}/rollback",
    response_model=ApiResponse[RollbackResponse],
    summary="Rollback to a specific commit",
)
async def rollback(
    project_id: str,
    body: RollbackRequest,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    await run_in_threadpool(ensure_write_access, authorization, current_user, project_id)

    from src.version_engine.write_engine.engine import VersionWriteEngine
    from src.version_engine.domain.intents import RollbackIntent

    who = f"user:{current_user.user_id}"
    engine = VersionWriteEngine(repo_manager)

    try:
        result = await engine.rollback(RollbackIntent(
            project_id=project_id,
            scope_path="",
            actor=who,
            source_channel="papi",
            target_commit_id=body.target_commit_id,
            message=f"rollback to {body.target_commit_id}",
        ))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {e}")

    return ApiResponse.success(data=RollbackResponse(
        project_id=project_id,
        new_commit_id=result.commit_id,
        rolled_back_to=body.target_commit_id,
    ))
