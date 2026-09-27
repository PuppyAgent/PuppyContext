"""
PuppyOneAuthenticator — version access authentication adapter

Maps PuppyOne's authentication system to the version access context:
  - JWT Bearer → human ProjectGrant + Project root view bounded by role
  - Access Key → connection + restricted repo scope

Supports:
  - Key revocation (revoked access points are rejected)
  - User identity binding via X-PuppyOne-User header
  - Channel pause enforcement via X-Puppy-Client header (cli):
    when present, the resolved scope's connector for that channel is
    consulted and the request is rejected with 403 if status='paused'.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from src.config import settings
from src.exceptions import ErrorCode
from src.infra.supabase.client import SupabaseClient
from src.platform.auth.dependencies import security
from src.platform.authorization.service import redacted_project_ref
from src.platform.repository_target.models import (
    ProjectRootTarget,
    ResolvedRepositoryView,
    ScopeTarget,
)
from src.repo.models import ResolvedScopeCredential
from src.utils.logger import log_error, log_warning
from src.version_engine.admission.channel_pause import enforce_channel_pause
from src.version_engine.infrastructure.supabase.scope_repository import (
    resolve_scope_access_credential,
)


class PuppyOneAuthenticator:
    """Resolve PuppyOne credentials to a version access context."""

    def __init__(self, supabase: SupabaseClient):
        # Held as the typed wrapper, not the raw supabase-py client, so
        # repository helpers (which take the wrapper) are usable here
        # without re-wrapping at every call site. L2 must not assemble
        # its own SQL; lookups go through infrastructure/.
        self._supabase = supabase

    def authenticate(self, token: str, project_id: str,
                     user_identity: str = "") -> dict:
        """Resolve a Bearer token to version access context.

        Args:
            token: Bearer token (JWT or access key)
            project_id: Target project ID
            user_identity: X-PuppyOne-User header value. Threaded onto the
                returned auth context as `_user_identity` so downstream
                handlers / hooks / audit logs can attribute the operation
                to the actual operator (the cli/agent identity behind the
                key). The strict per-key binding enforcement that this
                value used to drive moved to the new
                Project authorization boundary; this value is an identity
                hint, not an authorization gate.
            user_identity: X-PuppyOne-User header value (for identity binding)

        Returns a typed Project-root view for Human JWTs or a typed
        ``RuntimeGrant`` for machine credentials.
        """
        if settings.SKIP_AUTH:
            # config.py.enforce_skip_auth_safety guarantees APP_ENV is
            # dev/test if SKIP_AUTH is True; this assert is deep-defense in
            # case the validator is ever bypassed (mock, monkey-patched test).
            if settings.APP_ENV not in {"development", "test"}:
                log_error(
                    f"[Auth] SKIP_AUTH=True with APP_ENV={settings.APP_ENV!r}: "
                    f"config validator was bypassed; refusing to skip auth"
                )
                raise HTTPException(
                    status_code=500,
                    detail="Server misconfigured: SKIP_AUTH must not be active in this environment",
                )
            log_warning("SKIP_AUTH enabled — version auth returning mock user")
            target = ProjectRootTarget(project_id=project_id)
            return {
                "agent": "user:mock",
                "_repository_view": ResolvedRepositoryView(
                    target=target,
                    path_prefix="",
                    excludes=(),
                    max_mode="rw",
                ),
            }

        user = self._try_jwt(token)
        if user:
            # SECURITY (C-1): JWT alone is not sufficient — caller must also
            # be a member of the target project. Without this check, ANY
            # logged-in user could read/write the version tree of ANY project
            # by changing project_id in the URL.
            grant = self._resolve_project_grant(user["user_id"], project_id)
            if grant is None:
                log_warning(
                    f"[Auth] JWT user {user['user_id']} attempted version access "
                    f"to project {project_id} without membership"
                )
                raise HTTPException(
                    status_code=403,
                    detail="Not a member of this project",
                )
            target = ProjectRootTarget(project_id=project_id)
            return {
                "agent": f"user:{user['user_id']}",
                "_repository_view": ResolvedRepositoryView(
                    target=target,
                    path_prefix="",
                    excludes=(),
                    max_mode="rw",
                ),
                "_project_grant": grant,
                "_user_identity": user_identity,
            }

        try:
            resolved_credential = self._try_access_key(token, project_id)
        except Exception as error:
            log_error(
                "[Auth] credential target lookup failed "
                f"error_type={type(error).__name__}"
            )
            raise HTTPException(
                status_code=503,
                detail="Repository view is temporarily unavailable",
                headers={
                    "X-PuppyOne-Error-Code": str(
                        ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE.value
                    )
                },
            ) from error
        if resolved_credential:
            scope = resolved_credential.scope
            from src.platform.authorization.models import (
                RuntimeGrant,
                RuntimeMode,
                RuntimePrincipal,
            )
            target = ScopeTarget(
                project_id=scope.project_id,
                scope_id=scope.id,
            )
            max_mode = scope.max_mode

            runtime_grant = RuntimeGrant(
                principal=RuntimePrincipal(
                    principal_id=resolved_credential.credential_id,
                    credential_kind=resolved_credential.credential_type,
                ),
                target=target,
                repository_view=ResolvedRepositoryView(
                    target=target,
                    path_prefix=scope.path,
                    excludes=tuple(scope.exclude),
                    max_mode=max_mode,
                ),
                mode=RuntimeMode(max_mode),
            )
            return {
                "agent": f"scope:{scope.id}",
                "_runtime_grant": runtime_grant,
                "_credential_id": resolved_credential.credential_id,
                "_access_surface_id": resolved_credential.access_surface_id,
                "_user_identity": user_identity,
            }

        raise HTTPException(status_code=401, detail="Invalid version credentials")

    def _try_jwt(self, token: str) -> dict | None:
        try:
            from src.platform.auth.service import AuthService
            # AuthService expects the *underlying* supabase-py ``Client``
            # (which exposes ``.auth.get_claims`` for the JWKS fallback),
            # not our ``SupabaseClient`` wrapper. Passing the wrapper
            # silently falls back to the local JWT path until the JWKS
            # branch is reached, then crashes with
            # ``'SupabaseClient' object has no attribute 'auth'`` and the
            # caller treats every JWT as invalid.
            auth_svc = AuthService(SupabaseClient().client)
            user = auth_svc.get_current_user(token)
            return {"user_id": user.user_id}
        except HTTPException:
            # Expected: invalid/expired JWT → not a JWT, try next method
            return None
        except Exception as e:
            log_error(
                "[Auth] Unexpected JWT auth error "
                f"error_type={type(e).__name__}"
            )
            return None

    def _resolve_project_grant(self, user_id: str, project_id: str):
        """Resolve the canonical human ProjectGrant, failing closed."""
        try:
            from src.platform.authorization.factory import build_authorization_service

            return build_authorization_service(
                self._supabase.client
            ).resolve_project_grant(project_id, user_id)
        except Exception as e:
            # Fail closed: if the access check itself errors, deny access.
            log_error(
                "[Auth] Project access check failed "
                f"project_ref={redacted_project_ref(project_id)} "
                f"error_type={type(e).__name__}"
            )
            return None

    def _try_access_key(
        self,
        key: str,
        project_id: str,
    ) -> ResolvedScopeCredential | None:
        """Resolve an access key through canonical access-surface credentials.

        Access Surfaces own machine credentials; Scope is only the exact path
        target. There is no config or Scope-column fallback. Storage work lives
        in ``resolve_scope_access_credential`` so identity remains a policy
        layer and database failures do not become false authentication misses.
        """
        resolved = resolve_scope_access_credential(self._supabase, key)
        if resolved is None:
            return None
        if resolved.project_id != project_id:
            log_warning(
                "[Auth] access_key project mismatch (access surface scope) "
                f"requested_project_ref={redacted_project_ref(project_id)} "
                f"credential_project_ref={redacted_project_ref(resolved.project_id)}"
            )
            return None
        return resolved

    # ── Key management ──
    #
    # Key revocation uses access_surface_credentials.status. Credentials are
    # issued and rotated through an explicit Access Surface, never a Scope row.


def get_version_auth(
    request: Request,
    project_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """FastAPI dependency: extract and verify version access context.

    Two-stage gate:
      1. Resolve the Bearer token (JWT or access_key) → auth context with
         a scope binding. This is the existing identity check.
      2. If the request advertises a channel via X-Puppy-Client (e.g.
         'cli'), consult that channel's connector for the
         resolved scope and reject with 403 when status='paused'.

    Stage 2 is deliberately opt-in via the header so that older
    CLI / daemon installs that don't send X-Puppy-Client continue to
    work unchanged. The "Pause" toggle in the access-page UI becomes a
    hard gate progressively as clients roll out the header — for the
    in-app agent path the same enforcement happens inside the agent
    chat router (see src/platform/access/adapters/agent/chat/...).
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    user_identity = request.headers.get("x-puppyone-user", "")
    authenticator = PuppyOneAuthenticator(SupabaseClient())
    auth = authenticator.authenticate(
        credentials.credentials, project_id, user_identity=user_identity,
    )

    enforce_channel_pause(
        auth, request.headers.get("x-puppy-client"),
        log_prefix="[Auth]",
    )

    return auth
