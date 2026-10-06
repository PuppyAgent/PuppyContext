"""
Project Router

Provides REST API endpoints for project CRUD operations.
"""

import asyncio

from fastapi import APIRouter, Depends, Query, Response, status

from src.common_schemas import ApiResponse
from src.infra.supabase.dependencies import get_supabase_client
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
from src.platform.idempotency import mark_idempotency_replay, require_idempotency_key
from src.platform.organization.dependencies import resolve_org_ids
from src.platform.project.control_plane import ProjectControlPlaneService
from src.platform.project.control_plane_dependencies import get_project_control_plane_service
from src.platform.project.dependencies import get_project_service
from src.platform.project.git_view import ProjectGitViewService
from src.platform.project.orchestration import create_project_with_tree
from src.platform.project.presenters import project_to_out
from src.platform.project.readiness import ProjectReadinessService
from src.platform.project.schemas import (
    AddProjectMember,
    ProjectAuthorizationOut,
    ProjectCreate,
    ProjectDeletionOut,
    ProjectMemberOut,
    ProjectOut,
    ProjectUpdate,
    UpdateProjectMemberRole,
)
from src.platform.project.service import ProjectService
from src.platform.project.write_lease import (
    ProjectWriteLeaseFactory,
    get_project_write_lease_factory,
)
from src.platform.repository_target.protocol import require_repository_target_contract
from src.platform.template_registry.dependencies import (
    get_template_registry_service,
)
from src.platform.template_registry.exceptions import TemplateRegistryError
from src.platform.template_registry.http_errors import registry_http_exception
from src.platform.template_registry.schemas import TemplateDetail, TemplateSummary
from src.platform.template_registry.service import TemplateRegistryService
from src.version_engine.bootstrap.dependencies import (
    get_repo_manager,
    get_version_write_engine,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.write_engine.engine import VersionWriteEngine

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Internal server error"},
    },
)


def get_project_readiness_service() -> ProjectReadinessService:
    return ProjectReadinessService()


def get_project_git_view_service(
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
) -> ProjectGitViewService:
    return ProjectGitViewService(repo_manager)


def _count_user_access_points(project_ids: list[str]) -> dict[str, int]:
    """Count user-created entry points, excluding built-in connection methods."""

    if not project_ids:
        return {}
    sb = get_supabase_client()
    connection_rows = (
        sb.table("synchronize_bindings").select("project_id").in_("project_id", project_ids).execute()
    ).data or []
    from src.platform.access.surface_repository import AccessSurfaceRepository

    access_counts = AccessSurfaceRepository(sb).count_by_projects_and_kinds(
        project_ids, ["mcp", "sandbox"]
    )
    counts: dict[str, int] = {}
    for row in connection_rows:
        pid = row["project_id"]
        counts[pid] = counts.get(pid, 0) + 1
    for pid, count in access_counts.items():
        counts[pid] = counts.get(pid, 0) + count
    return counts


def _legacy_template_summary(template: TemplateSummary) -> dict[str, object]:
    """Preserve the pre-Registry template card wire shape during migration."""

    data = template.model_dump(mode="json", exclude_none=True)
    data["cover"] = data.get("cover_url")
    return data


def _legacy_template_detail(template: TemplateDetail) -> dict[str, object]:
    data = template.model_dump(mode="json", exclude_none=True)
    data["cover"] = data.get("cover_url")
    data["version"] = template.current_release.version
    data["preview_doc"] = (
        template.preview_document.model_dump(mode="json") if template.preview_document else None
    )
    return data


@router.get(
    "/",
    response_model=ApiResponse[list[ProjectOut]],
    summary="List projects",
    description="Get project metadata under the specified organization.",
    response_description="Returns all projects of the organization",
    status_code=status.HTTP_200_OK,
)
def list_projects(
    org_id: str | None = Query(
        None,
        description="Organization ID (if omitted, returns projects from all user organizations)",
    ),
    project_service: ProjectService = Depends(get_project_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
    include_access_counts: bool = True,
):
    # Sync handler: FastAPI runs it in a threadpool, so the blocking (sync)
    # Supabase calls below don't stall the event loop. No `await` in this body.
    oids = resolve_org_ids(org_id, current_user.user_id)

    all_projects = []
    for oid in oids:
        all_projects.extend(project_service.get_by_org_id(oid))

    accessible = authorization.filter_accessible(all_projects, current_user.user_id)

    # Batch-fetch entry-point counts only for authorized Projects. Count user-created
    # integrations; CLI / Agent / Filesystem / Git Remote are built-in methods.
    project_ids = [str(project.id) for project, _grant in accessible]
    conn_counts = _count_user_access_points(project_ids) if include_access_counts else {}

    result = []
    for p, grant in accessible:
        result.append(
            project_to_out(
                p,
                grant,
                access_point_count=conn_counts.get(str(p.id), 0) if include_access_counts else None,
            )
        )
    return ApiResponse.success(data=result, message="Project list retrieved successfully")


@router.get(
    "/templates/list",
    response_model=ApiResponse[list[dict[str, object]]],
    summary="List available project templates",
    description="Returns metadata for all available project templates.",
    status_code=status.HTTP_200_OK,
)
async def list_project_templates(
    registry: TemplateRegistryService = Depends(get_template_registry_service),
):
    """Compatibility alias for older clients; backed by the active provider."""

    try:
        catalog = await registry.catalog(limit=100)
    except TemplateRegistryError as exc:
        raise registry_http_exception(exc) from exc
    return ApiResponse.success(
        data=[_legacy_template_summary(item) for item in catalog.templates],
        message="Templates retrieved",
    )


@router.get(
    "/templates/{template_id}",
    response_model=ApiResponse[dict[str, object]],
    summary="Get a single template's detail (metadata + file tree + rendered preview doc)",
    status_code=status.HTTP_200_OK,
)
async def get_project_template(
    template_id: str,
    registry: TemplateRegistryService = Depends(get_template_registry_service),
):
    """Compatibility alias for older clients; backed by the active provider."""

    try:
        detail = await registry.get_template(template_id)
    except TemplateRegistryError as exc:
        raise registry_http_exception(exc) from exc
    return ApiResponse.success(
        data=_legacy_template_detail(detail),
        message="Template retrieved",
    )


@router.get(
    "/{project_id}",
    response_model=ApiResponse[ProjectOut],
    summary="Get project details",
    description="Get project details by project ID, including root directory entries.",
    response_description="Returns detailed project information",
    status_code=status.HTTP_200_OK,
)
def get_project(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_READ)),
):
    project = authorized.project
    conn_count = _count_user_access_points([str(project.id)]).get(str(project.id), 0)
    return ApiResponse.success(
        data=project_to_out(project, authorized.grant, access_point_count=conn_count),
        message="Project retrieved successfully",
    )


@router.get(
    "/{project_id}/authorization",
    response_model=ApiResponse[ProjectAuthorizationOut],
    summary="Get the current user's canonical Project grant",
)
def get_project_authorization(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_READ)),
):
    grant = authorized.grant
    return ApiResponse.success(
        data=ProjectAuthorizationOut(
            project_id=grant.project_id,
            org_id=grant.org_id,
            **grant.as_api_fields(),
        )
    )


@router.post(
    "/",
    response_model=ApiResponse[ProjectOut],
    summary="Create project",
    description=(
        "Create one canonical empty Project in the explicitly selected Organization. "
        "Template instantiation and content publication are separate operations."
    ),
    response_description="Returns the created project information",
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    payload: ProjectCreate,
    response: Response,
    idempotency_key: str = Depends(require_idempotency_key),
    control_plane: ProjectControlPlaneService = Depends(get_project_control_plane_service),
    entitlement_service: EntitlementService = Depends(get_entitlement_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    version_engine: VersionWriteEngine = Depends(get_version_write_engine),
    write_lease_factory: ProjectWriteLeaseFactory = Depends(
        get_project_write_lease_factory
    ),
    current_user: CurrentUser = Depends(get_current_user),
):
    # The RPC is the authoritative membership + quota admission point.  Keep
    # the caller-selected tenant explicit; never reinterpret it as "first org".
    resolved_org_id = payload.org_id
    project_limit = await asyncio.to_thread(
        entitlement_service.enforced_limit_value,
        resolved_org_id,
        "projects.max",
    )
    result = await create_project_with_tree(
        control_plane=control_plane,
        version_engine=version_engine,
        operation_key=idempotency_key,
        name=payload.name,
        description=payload.description,
        org_id=resolved_org_id,
        created_by=current_user.user_id,
        project_limit=project_limit,
        publication_mode="empty",
        source_fingerprint=(
            {"kind": "native-git-repository", "version": 1, **payload.repository.model_dump()}
            if payload.repository else {"kind": "empty-git-repository", "version": 1}
        ),
        native_repository=payload.repository.model_dump() if payload.repository else None,
        write_lease_factory=write_lease_factory,
    )
    project = result.project
    grant, access_point_counts = await asyncio.gather(
        asyncio.to_thread(
            authorization.authorize,
            str(project.id),
            current_user.user_id,
            ProjectAction.PROJECT_READ,
        ),
        asyncio.to_thread(_count_user_access_points, [str(project.id)]),
    )
    response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    mark_idempotency_replay(response, replayed=result.replayed)

    return ApiResponse.success(
        data=project_to_out(
            project,
            grant,
            access_point_count=access_point_counts.get(str(project.id), 0),
        ),
        message="Project created successfully",
    )


@router.put(
    "/{project_id}",
    response_model=ApiResponse[ProjectOut],
    summary="Update project",
    description="Update project information.",
    response_description="Returns the updated project information",
    status_code=status.HTTP_200_OK,
)
def update_project(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_MANAGE)),
    payload: ProjectUpdate = ...,
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = authorized.project

    updated_project = project_service.update(
        project_id=project.id,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        bound_git_branch=payload.bound_git_branch,
    )

    return ApiResponse.success(
        data=project_to_out(updated_project, authorized.grant),
        message="Project updated successfully",
    )


@router.delete(
    "/{project_id}",
    response_model=ApiResponse[ProjectDeletionOut],
    summary="Delete project",
    description=(
        "Remove Project authorization state and accept its durable object cleanup job. "
        "A 202 response does not claim that asynchronous object cleanup has completed."
    ),
    response_description="Deletion accepted with its durable cleanup status",
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_project(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_DELETE)),
    control_plane: ProjectControlPlaneService = Depends(get_project_control_plane_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = authorized.project
    result = control_plane.delete_project(
        project_id=project.id,
        actor_user_id=current_user.user_id,
    )
    return ApiResponse.success(
        data=ProjectDeletionOut.model_validate(result.as_dict()),
        message="Project deletion accepted",
    )


@router.post(
    "/{project_id}/initialization/abandon",
    response_model=ApiResponse[ProjectDeletionOut],
    summary="Abandon an empty Project initialization",
    status_code=status.HTTP_202_ACCEPTED,
)
def abandon_project_initialization(
    project_id: str,
    response: Response,
    idempotency_key: str = Depends(require_idempotency_key),
    control_plane: ProjectControlPlaneService = Depends(get_project_control_plane_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = control_plane.abandon_initialization(
        project_id=project_id,
        operation_key=idempotency_key,
        actor_user_id=current_user.user_id,
    )
    mark_idempotency_replay(response, replayed=result.replayed)
    if result.replayed and result.status == "completed":
        response.status_code = status.HTTP_200_OK
    return ApiResponse.success(
        data=ProjectDeletionOut.model_validate(result.as_dict()),
        message=(
            "Project deletion completed"
            if result.status == "completed"
            else "Project initialization abandonment accepted"
        ),
    )


@router.post(
    "/{project_id}/seed",
    response_model=ApiResponse[dict],
    summary="Write default seed content",
    description="Write Getting Started + Guides default content for an existing project.",
    status_code=status.HTTP_201_CREATED,
)
async def seed_project(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.CONTENT_WRITE)),
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    project = authorized.project
    from src.platform.project.seed_content import seed_default_content

    result = await seed_default_content(
        project_id=str(project.id),
        created_by=current_user.user_id,
    )
    return ApiResponse.success(data=result, message="Seed content created")


# ── Project Members ──


@router.get(
    "/{project_id}/members",
    response_model=ApiResponse[list[ProjectMemberOut]],
    summary="List project members",
)
def list_project_members(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.MEMBERS_READ)),
    project_service: ProjectService = Depends(get_project_service),
):
    rows = project_service.list_project_members(authorized.project.id)
    result = []
    for row in rows:
        profile = row.get("profiles") or {}
        result.append(
            ProjectMemberOut(
                id=row["id"],
                user_id=row["user_id"],
                email=profile.get("email"),
                display_name=profile.get("display_name"),
                avatar_url=profile.get("avatar_url"),
                role=row["role"],
                created_at=row["created_at"],
            )
        )
    return ApiResponse.success(data=result)


@router.post(
    "/{project_id}/members",
    response_model=ApiResponse[None],
    summary="Add project member",
    status_code=status.HTTP_201_CREATED,
)
def add_project_member(
    payload: AddProjectMember,
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.MEMBERS_MANAGE)),
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    project_service.add_project_member(
        authorized.project.id,
        payload.user_id,
        payload.role,
        granted_by=current_user.user_id,
    )
    return ApiResponse.success(message="Member added")


@router.put(
    "/{project_id}/members/{target_user_id}/role",
    response_model=ApiResponse[None],
    summary="Update project member role",
)
def update_project_member_role(
    target_user_id: str,
    payload: UpdateProjectMemberRole,
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.MEMBERS_MANAGE)),
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    project_service.update_project_member_role(
        authorized.project.id,
        target_user_id,
        payload.role,
        actor_user_id=current_user.user_id,
    )
    return ApiResponse.success(message="Role updated")


@router.delete(
    "/{project_id}/members/{target_user_id}",
    response_model=ApiResponse[None],
    summary="Remove project member",
)
def remove_project_member(
    target_user_id: str,
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.MEMBERS_MANAGE)),
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    project_service.remove_project_member(
        authorized.project.id,
        target_user_id,
        actor_user_id=current_user.user_id,
    )
    return ApiResponse.success(message="Member removed")


# ── Project share link (MVP) ──
#
# Three endpoints implement the share-link primitive. Two are gated to
# owner/admin (view + rotate the token); the third — joining via a
# token — only requires being signed in, because the token itself is
# the auth artifact for the join action. Rotating the token is the
# revoke-all-outstanding-links mechanism.
#
# The share URL the frontend renders is composed there from
# ``NEXT_PUBLIC_API_URL`` / location.origin + ``/share/{token}``. We
# intentionally don't construct the URL server-side here so callers
# behind different reverse proxies can each derive their own host.


@router.get(
    "/{project_id}/share",
    response_model=ApiResponse[dict],
    summary="Get share link info (owner/admin only)",
)
def get_share_info(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.SHARE_MANAGE)),
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    info = project_service.get_share_info(authorized.project.id, current_user.user_id)
    return ApiResponse.success(data=info)


@router.post(
    "/{project_id}/share/rotate",
    response_model=ApiResponse[dict],
    summary="Rotate share token (revokes existing link)",
)
def rotate_share_token(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.SHARE_MANAGE)),
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    info = project_service.rotate_share_token(authorized.project.id, current_user.user_id)
    return ApiResponse.success(
        data=info,
        message="Share link rotated; previous link is no longer valid",
    )


@router.post(
    "/share/{token}/join",
    response_model=ApiResponse[dict],
    summary="Join a project via share link",
)
def join_via_share_token(
    token: str,
    project_service: ProjectService = Depends(get_project_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Accept the share link and join as ``viewer``.

    Idempotent: an existing member just receives their current role
    back. An invalid / rotated token returns 404 — exactly the same
    visible behaviour, so the link doesn't leak "this project exists"
    after revocation.
    """
    result = project_service.join_via_share_token(token, current_user.user_id)
    return ApiResponse.success(
        data=result,
        message=("Joined project" if result.get("newly_joined") else "Already a member"),
    )


@router.get(
    "/{project_id}/readiness",
    response_model=ApiResponse[dict],
    summary="Get Project repository Git and Claude readiness",
)
def get_project_readiness(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_READ)),
    readiness: ProjectReadinessService = Depends(get_project_readiness_service),
    _repository_contract: int = Depends(require_repository_target_contract),
):
    return ApiResponse.success(data=readiness.resolve(authorized.project.id).as_dict())


@router.get(
    "/{project_id}/git-view/health",
    response_model=ApiResponse[dict],
    summary="Get Project repository Git view health through the control plane",
)
def get_project_git_view_health(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_READ)),
    git_view: ProjectGitViewService = Depends(get_project_git_view_service),
    _repository_contract: int = Depends(require_repository_target_contract),
):
    """Return derived Git health using Human Project authorization.

    The smart-HTTP ``/git/.../health`` route intentionally remains a machine
    RuntimeGrant endpoint; a human JWT is never forwarded to that data plane.
    """

    return ApiResponse.success(
        data=git_view.health(
            str(authorized.project.id),
            content_write_allowed=authorized.grant.allows(ProjectAction.CONTENT_WRITE),
            cache_rebuild_allowed=authorized.grant.allows(ProjectAction.PROJECT_MANAGE),
        ),
        message="Project Git view health loaded",
    )


@router.post(
    "/{project_id}/git-view/rebuild-cache",
    response_model=ApiResponse[dict],
    summary="Rebuild Project repository Git view cache through the control plane",
)
def rebuild_project_git_view_cache(
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_MANAGE)),
    git_view: ProjectGitViewService = Depends(get_project_git_view_service),
    _repository_contract: int = Depends(require_repository_target_contract),
):
    """Rebuild both derived root-view cache variants from canonical facts."""

    return ApiResponse.success(
        data=git_view.rebuild(str(authorized.project.id)),
        message="Project Git view caches rebuilt from canonical facts",
    )
