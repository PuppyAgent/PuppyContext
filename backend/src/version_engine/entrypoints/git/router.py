"""Git protocol routes.

The router is intentionally thin: it resolves authentication and request
shape, then delegates protocol work to receive-pack/upload-pack modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from src.common_schemas import ApiResponse
from src.version_engine.adapters.git.execution import admission, run_owned, current_execution, MAX_PACK_BYTES, MAX_SECONDS
from src.config import settings
from src.exceptions import ErrorCode
from src.platform.repository_target.auth_context import repository_target_from_auth
from src.platform.repository_target.models import repository_target_scope_id
from src.utils.logger import log_info
from src.version_engine.adapters.git.health import git_view_health_payload
from src.version_engine.adapters.git.receive_pack import (
    receive_pack_response_from_path,
)
from src.version_engine.adapters.git.upload_pack import (
    info_refs_response,
    upload_pack_streaming_response,
)
from src.version_engine.admission.repo_facade import (
    RepositoryViewResolutionError,
    repo_facade_from_auth,
)
from src.version_engine.admission.target import admit_target
from src.version_engine.bootstrap.dependencies import get_repo_manager
from src.version_engine.derived.git_transport_cache import rebuild_git_transport_view
from src.version_engine.entrypoints.git.auth import (
    request_actor,
    resolve_git_project_auth,
    resolve_git_scope_auth,
)
from src.version_engine.entrypoints.git.auth import (
    resolve_git_access_point as _resolve_git_access_point,
)
from src.version_engine.entrypoints.git.native import select_native_git_endpoint
from src.version_engine.entrypoints.http.access_point import resolve_access_point
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager

router = APIRouter(prefix="/git")


async def resolve_git_access_point(access_key: str, request: Request) -> tuple[str, dict]:
    """Shared legacy auth with a stable router-level resolver injection seam."""

    return await _resolve_git_access_point(
        access_key,
        request,
        resolver=resolve_access_point,
    )


def _git_audit_detail(
    *,
    auth: dict,
    entry_point: str,
    actor: str,
    project_id: str = "",
) -> dict:
    facade = repo_facade_from_auth(
        project_id or auth.get("_project_id", ""),
        auth,
        kind=("access_point" if entry_point == "access_key_git_remote" else "project_git_remote"),
    )
    scope_id = repository_target_scope_id(repository_target_from_auth(auth)) or ""
    runtime_grant = auth.get("_runtime_grant")
    runtime_principal = getattr(runtime_grant, "principal", None)
    runtime_principal_id = auth.get("_credential_id") or getattr(
        runtime_principal, "principal_id", ""
    )
    runtime_credential_kind = getattr(runtime_principal, "credential_kind", "")
    return {
        "source_channel": "access_git",
        "protocol": "git",
        "entry_point": entry_point,
        "remote": (
            "Access key Git remote"
            if entry_point == "access_key_git_remote"
            else "Project Git remote"
        ),
        **facade.audit_detail(),
        "scope_id": scope_id,
        # The Git username / actor headers are client-supplied attribution.
        # Preserve them for commit UX, but always record the immutable runtime
        # principal that actually authorized the request.
        "actor": actor,
        "runtime_principal_id": runtime_principal_id,
        "runtime_credential_kind": runtime_credential_kind,
        "access_surface_id": auth.get("_access_surface_id", ""),
        "credential_user_id": auth.get("_credential_user_id"),
    }


async def _record_git_fetch_audit(
    *,
    repo,
    auth: dict,
    actor: str,
    entry_point: str,
    project_id: str = "",
) -> None:
    detail = {
        **_git_audit_detail(
            auth=auth,
            entry_point=entry_point,
            actor=actor,
            project_id=project_id,
        ),
        "service": "upload-pack",
    }
    await asyncio.to_thread(repo.record_audit, "git_fetch", actor, detail)


def _git_receive_max_body_bytes(project_id: str) -> int:
    from src.platform.entitlements.service import EntitlementService
    from src.platform.project.repository import ProjectRepositorySupabase

    project = ProjectRepositorySupabase().get_by_id(project_id)
    if project is None:
        return _effective_receive_pack_cap(None)
    limit = EntitlementService().enforced_limit_value(
        project.org_id,
        "upload.max_batch_bytes",
    )
    return _effective_receive_pack_cap(limit)


def _effective_receive_pack_cap(entitlement_limit: int | None) -> int:
    """Choose the smaller finite plan limit and mandatory infrastructure cap."""
    hard_cap = int(settings.GIT_MAX_RECEIVE_PACK_BYTES)
    if hard_cap <= 0:
        raise RuntimeError("GIT_MAX_RECEIVE_PACK_BYTES must be positive")
    if entitlement_limit is None:
        return hard_cap
    plan_cap = int(entitlement_limit)
    return min(plan_cap, hard_cap) if plan_cap > 0 else hard_cap


async def _spool_git_request_body(
    request: Request,
    *,
    max_body_bytes: int | None = None,
) -> Path:
    """Spool a Git RPC request body to disk.

    Large Git pushes may arrive as chunked transfer bodies. Keeping them off
    the Python heap lets stock Git consume the exact decoded request bytes
    without requiring users to tune client-side buffering.

    ``max_body_bytes`` fail-fast caps the compressed request body so a hostile
    client cannot fill the spool disk (or feed an unbounded pack downstream).
    Streaming is aborted and the partial file removed as soon as the cap is
    crossed, raising HTTP 413.
    """

    content_length = request.headers.get("content-length")
    if max_body_bytes is not None and content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = -1
        if declared_size > max_body_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Git request body is {declared_size} bytes; "
                    f"max allowed is {max_body_bytes} bytes"
                ),
            )

    tmp = tempfile.NamedTemporaryFile(
        prefix="puppyone-git-rpc-",
        delete=False,
    )
    try:
        total = 0
        async for chunk in request.stream():
            if chunk:
                total += len(chunk)
                if max_body_bytes is not None and total > max_body_bytes:
                    raise HTTPException(
                        status_code=400,
                        detail=(f"Git request body exceeds max allowed {max_body_bytes} bytes"),
                    )
                tmp.write(chunk)
        tmp.close()
        return Path(tmp.name)
    except BaseException:
        name = tmp.name
        tmp.close()
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def _unlink_temp(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


async def _resolve_canonical_git_auth(
    project_id: str,
    scope_id: str | None,
    request: Request,
) -> dict:
    if scope_id is None:
        return await resolve_git_project_auth(project_id, request)
    return await resolve_git_scope_auth(project_id, scope_id, request)


@dataclass(frozen=True, slots=True)
class _ResolvedGitTarget:
    project_id: str
    auth: dict
    entry_point: str
    facade_kind: str
    log_prefix: str


async def _resolve_canonical_target(
    project_id: str,
    scope_id: str | None,
    request: Request,
) -> _ResolvedGitTarget:
    return _ResolvedGitTarget(
        project_id=project_id,
        auth=await _resolve_canonical_git_auth(project_id, scope_id, request),
        entry_point="project_git_remote",
        facade_kind="project_git_remote",
        log_prefix="[GitProject]",
    )


async def _resolve_access_point_target(
    access_key: str,
    request: Request,
) -> _ResolvedGitTarget:
    project_id, auth = await resolve_git_access_point(access_key, request)
    scope_id = repository_target_scope_id(repository_target_from_auth(auth)) or ""
    # The compatibility path contains the credential, so telemetry is emitted
    # only after successful resolution and never includes the request path or
    # raw Project/Scope identifiers. This is the rollout-removal counter.
    log_info(
        "[GitLegacy] route=access_key_git_remote outcome=accepted "
        f"project_ref={_telemetry_ref(project_id)} "
        f"scope_ref={_telemetry_ref(scope_id)}"
    )
    return _ResolvedGitTarget(
        project_id=project_id,
        auth=auth,
        entry_point="access_key_git_remote",
        facade_kind="access_point",
        log_prefix="[GitAP]",
    )


def _telemetry_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _repo_and_facade(target: _ResolvedGitTarget, repo_manager: VersionRepoManager):
    repo = repo_manager.get_server_repo(target.project_id)
    try:
        facade = repo_facade_from_auth(
            target.project_id,
            target.auth,
            kind=target.facade_kind,
            scope_backend=(
                repo_manager.get_scope_backend(target.project_id)
                if target.entry_point == "access_key_git_remote"
                else None
            ),
        )
    except RepositoryViewResolutionError as exc:
        raise HTTPException(
            status_code=503,
            detail="Repository view is temporarily unavailable",
            headers={
                "X-PuppyOne-Error-Code": str(
                    ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE.value
                )
            },
        ) from exc
    return repo, facade


async def _native_call(function, *args, disconnect=None, **kwargs):
    try:
        if current_execution.get() is None and function is not select_native_git_endpoint:
            with admission() as retain:
                return retain(await run_owned(function, *args, cancel_when=disconnect, **kwargs))
        return await run_owned(function, *args, cancel_when=disconnect, **kwargs)
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(403, "Repository action denied") from exc
    except ValueError as exc:
        raise HTTPException(400, "Invalid Git request") from exc
    except Exception as exc:
        raise HTTPException(503, "Repository is temporarily unavailable") from exc


async def _native_endpoint(target, repo_manager):
    return await _native_call(
        select_native_git_endpoint, repo_manager, target.project_id, target.auth,
        legacy_route=target.entry_point == "access_key_git_remote",
    )


async def _git_info_refs_for_target(
    target: _ResolvedGitTarget,
    service: str,
    repo_manager: VersionRepoManager,
    protocol: str = "",
):
    native = await _native_endpoint(target, repo_manager)
    if native is not None:
        return await _native_call(native.info_refs, service, protocol=protocol)
    repo, facade = _repo_and_facade(target, repo_manager)
    return await asyncio.to_thread(
        info_refs_response,
        repo,
        service,
        facade.scope_path,
        list(facade.excludes),
    )


@router.get("/{project_id}.git/info/refs")
@router.get("/{project_id}/scopes/{scope_id}.git/info/refs")
async def git_info_refs(
    project_id: str,
    service: str,
    request: Request,
    scope_id: str | None = None,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Advertise a canonical Project-root or scoped Git endpoint."""

    return await _git_info_refs_for_target(
        await _resolve_canonical_target(project_id, scope_id, request),
        service,
        repo_manager,
        protocol=request.headers.get("git-protocol", ""),
    )


@router.get("/ap/{access_key}.git/info/refs")
async def git_ap_info_refs(
    access_key: str,
    service: str,
    request: Request,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Advertise Git refs through an Access Point-bound scope."""

    return await _git_info_refs_for_target(
        await _resolve_access_point_target(access_key, request),
        service,
        repo_manager,
        protocol=request.headers.get("git-protocol", ""),
    )


async def _git_health_for_target(
    target: _ResolvedGitTarget,
    repo_manager: VersionRepoManager,
):
    native = await _native_endpoint(target, repo_manager)
    if native is not None:
        return ApiResponse.success(data=await _native_call(native.health), message="Git view health loaded")
    repo, facade = _repo_and_facade(target, repo_manager)
    return ApiResponse.success(
        data=git_view_health_payload(
            repo,
            project_id=target.project_id,
            scope_path=facade.scope_path,
            scope_excludes=list(facade.excludes),
            read_only=facade.read_only,
        ),
        message="Git view health loaded",
    )


@router.get("/ap/{access_key}.git/health")
async def git_ap_health(
    access_key: str,
    request: Request,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Return product-facing Git view health for an Access Point remote."""

    return await _git_health_for_target(
        await _resolve_access_point_target(access_key, request),
        repo_manager,
    )


@router.get("/{project_id}.git/health")
@router.get("/{project_id}/scopes/{scope_id}.git/health")
async def git_project_health(
    project_id: str,
    request: Request,
    scope_id: str | None = None,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Return Git view health for a project's root remote.

    The Access Point variant has had a health endpoint for a while; the
    project-root Git remote has the same failure modes (empty / healthy /
    history_degraded / current_corrupt) and the same need to expose them
    for self-diagnosis. Same auth + same payload shape as the AP route —
    just resolved against the Project root view instead of an
    access-key-bound Scope view.
    """

    return await _git_health_for_target(
        await _resolve_canonical_target(project_id, scope_id, request),
        repo_manager,
    )


async def _rebuild_both_variants(repo, facade) -> dict:
    """Rebuild both cache variants for one view and return the combined payload.

    Each view has two cache shapes: full-history-with-blobs (clone/fetch
    serves this) and receive-boundary-without-blobs (push advertisement
    serves this). If we only rebuilt one, the other would stay cold and
    the next request in that direction would re-warm — making the
    rebuild endpoint only partially effective.
    """
    rebuilt_full = await asyncio.to_thread(
        rebuild_git_transport_view,
        repo,
        scope_path=facade.scope_path,
        scope_excludes=list(facade.excludes),
        follow_history=True,
        include_blobs=True,
    )
    rebuilt_boundary = await asyncio.to_thread(
        rebuild_git_transport_view,
        repo,
        scope_path=facade.scope_path,
        scope_excludes=list(facade.excludes),
        follow_history=False,
        include_blobs=False,
    )
    return {"variants": [rebuilt_full, rebuilt_boundary]}


async def _git_rebuild_for_target(
    target: _ResolvedGitTarget,
    request: Request,
    repo_manager: VersionRepoManager,
):
    native = await _native_endpoint(target, repo_manager)
    if native is not None:
        return ApiResponse.success(data=await _native_call(native.rebuild), message="Native disposable caches require no persistent rebuild")
    repo, facade = _repo_and_facade(target, repo_manager)
    admit_target(
        target.auth,
        facade,
        action="write",
        source_channel="access_git",
        channel_header=request.headers.get("x-puppy-client"),
        log_prefix=f"{target.log_prefix}[rebuild]",
    )
    return ApiResponse.success(
        data=await _rebuild_both_variants(repo, facade),
        message="Git view caches rebuilt from canonical facts",
    )


@router.post("/ap/{access_key}.git/rebuild-cache")
async def git_ap_rebuild_cache(
    access_key: str,
    request: Request,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Drop and rewarm the Git view cache for an Access Point remote.

    The architecture doc promises "if the cache is missing or unhealthy,
    it can be rebuilt from committed Version Engine facts" — this is
    that public trigger. Goes through ``admit_target`` so mode +
    channel-pause checks run in one place; ``write`` action because
    rebuild mutates the on-disk per-view bare repo. Read-only callers
    can still get diagnostics via ``/health``.
    """
    return await _git_rebuild_for_target(
        await _resolve_access_point_target(access_key, request),
        request,
        repo_manager,
    )


@router.post("/{project_id}.git/rebuild-cache")
@router.post("/{project_id}/scopes/{scope_id}.git/rebuild-cache")
async def git_project_rebuild_cache(
    project_id: str,
    request: Request,
    scope_id: str | None = None,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Drop and rewarm the project-root Git view cache. See
    ``git_ap_rebuild_cache`` for the shared invariants."""

    return await _git_rebuild_for_target(
        await _resolve_canonical_target(project_id, scope_id, request),
        request,
        repo_manager,
    )


async def _native_rpc(native, request, *, upload):
    # Admit before reading a request body. Keep the slot until a fetch response
    # is sent/aborted, so slow clients cannot accumulate disk spools.
    with admission() as retain:
        async with asyncio.timeout(MAX_SECONDS):
            path = await _spool_git_request_body(request, max_body_bytes=(16 * 1024**2 if upload else MAX_PACK_BYTES))
            try:
                response = await _native_call(native.upload if upload else native.receive, path,
                                              disconnect=request.is_disconnected,
                                              **({'protocol': request.headers.get('git-protocol', '')} if upload else {}))
                return retain(response)
            finally:
                _unlink_temp(path)


async def _git_receive_pack_for_target(
    target: _ResolvedGitTarget,
    request: Request,
    repo_manager: VersionRepoManager,
):
    native = await _native_endpoint(target, repo_manager)
    if native is not None:
        return await _native_rpc(native, request, upload=False)
    max_body_bytes = await asyncio.to_thread(_git_receive_max_body_bytes, target.project_id)
    request_path = await _spool_git_request_body(
        request,
        max_body_bytes=max_body_bytes,
    )
    actor = request_actor(request, target.auth)
    try:
        repo, facade = _repo_and_facade(target, repo_manager)
        return await receive_pack_response_from_path(
            repo_manager=repo_manager,
            repo=repo,
            project_id=target.project_id,
            scope_path=facade.scope_path,
            scope_excludes=list(facade.excludes),
            actor=actor,
            request_path=request_path,
            read_only=facade.read_only,
            audit_detail=_git_audit_detail(
                auth=target.auth,
                entry_point=target.entry_point,
                actor=actor,
                project_id=target.project_id,
            ),
        )
    finally:
        _unlink_temp(request_path)


@router.post("/{project_id}.git/git-receive-pack")
@router.post("/{project_id}/scopes/{scope_id}.git/git-receive-pack")
async def git_receive_pack(
    project_id: str,
    request: Request,
    scope_id: str | None = None,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Receive a Git push and publish it through the version engine."""

    return await _git_receive_pack_for_target(
        await _resolve_canonical_target(project_id, scope_id, request),
        request,
        repo_manager,
    )


@router.post("/ap/{access_key}.git/git-receive-pack")
async def git_ap_receive_pack(
    access_key: str,
    request: Request,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Receive a Git push through an Access Point-bound scope."""

    return await _git_receive_pack_for_target(
        await _resolve_access_point_target(access_key, request),
        request,
        repo_manager,
    )


async def _git_upload_pack_for_target(
    target: _ResolvedGitTarget,
    request: Request,
    repo_manager: VersionRepoManager,
):
    native = await _native_endpoint(target, repo_manager)
    actor = request_actor(request, target.auth)
    if native is not None:
        await _record_git_fetch_audit(repo=native, auth=target.auth, actor=actor,
                                    entry_point=target.entry_point, project_id=target.project_id)
        return await _native_rpc(native, request, upload=True)
    repo, facade = _repo_and_facade(target, repo_manager)
    request_path = await _spool_git_request_body(request, max_body_bytes=settings.GIT_MAX_UPLOAD_PACK_BYTES or None)
    try:
        await _record_git_fetch_audit(
            repo=repo,
            auth=target.auth,
            actor=actor,
            entry_point=target.entry_point,
            project_id=target.project_id,
        )
        return await upload_pack_streaming_response(
            repo,
            facade.scope_path,
            list(facade.excludes),
            request_path,
        )
    except BaseException:
        _unlink_temp(request_path)
        raise


@router.post("/{project_id}.git/git-upload-pack")
@router.post("/{project_id}/scopes/{scope_id}.git/git-upload-pack")
async def git_upload_pack(
    project_id: str,
    request: Request,
    scope_id: str | None = None,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Serve a Git fetch/clone pack."""

    return await _git_upload_pack_for_target(
        await _resolve_canonical_target(project_id, scope_id, request),
        request,
        repo_manager,
    )


@router.post("/ap/{access_key}.git/git-upload-pack")
async def git_ap_upload_pack(
    access_key: str,
    request: Request,
    repo_manager: VersionRepoManager = Depends(get_repo_manager),
):
    """Serve a Git fetch/clone through an Access Point-bound scope."""

    return await _git_upload_pack_for_target(
        await _resolve_access_point_target(access_key, request),
        request,
        repo_manager,
    )
