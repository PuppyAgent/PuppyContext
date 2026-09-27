"""HTTP API for repository Scope CRUD.

Mounted at /api/v1/projects/{project_id}/scopes by main.py. Every endpoint
declares its canonical Project action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from src.common_schemas import ApiResponse
from src.platform.authorization.dependencies import AuthorizedProject, require_project_action
from src.platform.authorization.models import ProjectAction
from src.platform.repository_target.protocol import require_repository_target_contract
from src.repo.models import RepositoryScope
from src.repo.schemas import (
    ScopeAutoSuggestOut,
    ScopeIn,
    ScopeOut,
    ScopePatch,
)
from src.repo.scope_service import ScopeService

router = APIRouter(
    prefix="/projects/{project_id}/scopes",
    tags=["repository-scopes"],
    dependencies=[Depends(require_repository_target_contract)],
)


# ──────────────────────────────────────────────────────────────────────────
# DI
# ──────────────────────────────────────────────────────────────────────────

def get_scope_service() -> ScopeService:
    return ScopeService()


# ──────────────────────────────────────────────────────────────────────────
# Mappers
# ──────────────────────────────────────────────────────────────────────────

def _to_out(scope: RepositoryScope) -> ScopeOut:
    return ScopeOut(
        id=scope.id,
        project_id=scope.project_id,
        name=scope.name,
        path=scope.path,
        exclude=scope.exclude,
        max_mode=scope.max_mode,           # type: ignore[arg-type]
        created_at=scope.created_at,
        updated_at=scope.updated_at,
    )


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=ApiResponse[list[ScopeOut]],
    summary="List scopes for a project",
)
def list_scopes(
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.ACCESS_READ)
    ),
    service: ScopeService = Depends(get_scope_service),
):
    scopes = service.list_for_project(str(authorized.project.id))
    return ApiResponse.success(
        # Machine credentials are one-time reveal only. Ordinary reads never
        # reconstruct a bearer token from storage.
        data=[_to_out(s) for s in scopes],
        message="Scopes listed",
    )


@router.post(
    "",
    response_model=ApiResponse[ScopeOut],
    status_code=status.HTTP_201_CREATED,
    summary="Create a repository path Scope",
)
def create_scope(
    payload: ScopeIn,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.SCOPE_MANAGE)
    ),
    service: ScopeService = Depends(get_scope_service),
):
    scope = service.create(
        project_id=str(authorized.project.id),
        name=payload.name,
        path=payload.path,
        exclude=payload.exclude,
        max_mode=payload.max_mode,
    )
    return ApiResponse.success(data=_to_out(scope), message="Scope created")


@router.patch(
    "/{scope_id}",
    response_model=ApiResponse[ScopeOut],
    summary="Update name / exclude / max mode (path is immutable)",
)
def update_scope(
    scope_id: str,
    payload: ScopePatch,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.SCOPE_MANAGE)
    ),
    service: ScopeService = Depends(get_scope_service),
):
    existing = service.get(scope_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Scope not found")
    updated = service.update(
        scope_id,
        name=payload.name,
        exclude=payload.exclude,
        max_mode=payload.max_mode,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Scope not found after update")
    # Updating metadata never re-reveals a machine credential.  The raw token
    # is available only from create/regenerate responses.
    return ApiResponse.success(data=_to_out(updated), message="Scope updated")


@router.delete(
    "/{scope_id}",
    response_model=ApiResponse[None],
    summary="Delete a Scope and its exact-target resources",
)
def delete_scope(
    scope_id: str,
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.SCOPE_MANAGE)
    ),
    service: ScopeService = Depends(get_scope_service),
):
    existing = service.get(scope_id)
    if existing is None or existing.project_id != str(authorized.project.id):
        raise HTTPException(status_code=404, detail="Scope not found")
    # Refuse deletion while user-configured Surfaces or external Connections
    # still target the Scope. Standard Git/CLI Surfaces cascade with it.
    from src.platform.access.model_repository import AccessModelRepository
    conn_repo = AccessModelRepository()
    n_third_party = conn_repo.count_third_party_for_scope(scope_id)
    service.delete(scope_id, has_bound_connectors=n_third_party > 0)

    # Drop ``fs_path_index`` rows pinned to this scope's prefix —
    # otherwise a future scope created at the same path would inherit
    # stale rows pointing at the previous scope's blob hashes.
    # Best-effort: failure here is logged inside the helper and does
    # not bubble up because the scope is already gone.
    from src.version_engine.derived.path_index import (
        cleanup_fs_path_index_for_scope,
    )
    cleanup_fs_path_index_for_scope(str(authorized.project.id), existing.path or "")
    return ApiResponse.success(message="Scope deleted")


@router.post(
    "/auto-suggest",
    response_model=ApiResponse[ScopeAutoSuggestOut],
    summary="Suggest new scopes from current top-level folders",
)
def auto_suggest_scopes(
    authorized: AuthorizedProject = Depends(
        require_project_action(ProjectAction.CONTENT_READ)
    ),
    service: ScopeService = Depends(get_scope_service),
):
    """Reads the current version tree's top-level folders and returns those
    not already covered by an existing scope as proposed scope candidates."""
    from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
    ops = build_worker_version_engine_container().product_operations()
    try:
        entries = ops.list_dir(str(authorized.project.id), "")
    except Exception:
        entries = []
    folder_names = [e.name for e in entries if getattr(e, "type", None) == "folder"]
    suggestions = service.auto_suggest_from_tree(
        str(authorized.project.id), folder_names
    )
    return ApiResponse.success(
        data=ScopeAutoSuggestOut(suggestions=[
            ScopeIn(**s) for s in suggestions
        ]),
        message="Suggestions generated",
    )
