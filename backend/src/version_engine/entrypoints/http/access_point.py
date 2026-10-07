"""Resolve a Project-root filesystem credential through canonical Git authority."""

from fastapi import HTTPException

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ResolvedRepositoryView


def resolve_access_point(access_key: str) -> tuple[str, dict]:
    from src.infra.supabase.client import SupabaseClient
    from src.platform.repository_target.models import ProjectRootTarget
    from src.repo.access_credentials import AccessCredentialRepository

    try:
        resolved = AccessCredentialRepository(
            SupabaseClient().client
        ).resolve_git_runtime_credential(access_key)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Repository credential lookup unavailable"
        ) from exc
    if not resolved:
        raise HTTPException(status_code=401, detail="Invalid repository credential")
    if resolved.get("target_kind") != "project_root" or resolved.get("scope_id") is not None:
        raise HTTPException(status_code=501, detail={"code": "native_scope_not_available"})
    if resolved.get("path_prefix") or resolved.get("excludes"):
        raise HTTPException(status_code=403, detail="Invalid Project-root view")
    project_id = resolved["project_id"]
    target = ProjectRootTarget(project_id=project_id)
    grant = RuntimeGrant(
        principal=RuntimePrincipal(
            principal_id=resolved["credential_id"], credential_kind="git_http_token"
        ),
        target=target,
        repository_view=ResolvedRepositoryView(
            target=target, path_prefix="", excludes=(), max_mode=resolved["target_max_mode"]
        ),
        mode=RuntimeMode(resolved["effective_mode"]),
    )
    return project_id, {
        "agent": "git-credential:" + resolved["credential_id"],
        "_runtime_grant": grant,
        "_credential_id": resolved["credential_id"],
        "_access_surface_id": resolved["access_surface_id"],
        "_credential_user_id": resolved.get("user_id"),
        "_user_identity": "",
    }
