"""
Authentication dependency injection
"""


from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import settings
from src.exceptions import AuthException
from src.infra.supabase.dependencies import get_supabase_client
from src.platform.auth.initialization import UserInitializationService
from src.platform.auth.models import CurrentUser
from src.platform.auth.service import AuthService
from src.utils.logger import log_error, log_warning

security = HTTPBearer(auto_error=False)


def _assert_skip_auth_safe() -> None:
    """Refuse to honor SKIP_AUTH if APP_ENV is not dev/test.

    config.py.enforce_skip_auth_safety should already have crashed startup
    in this case. This runtime check is deep-defense in case the validator
    is bypassed (mocks, monkey-patches, settings hot-reload). Returning a
    mock user in production would expose every endpoint as anonymous.
    """
    if settings.APP_ENV not in {"development", "test"}:
        log_error(
            f"[Auth] SKIP_AUTH=True with APP_ENV={settings.APP_ENV!r}: "
            f"refusing to bypass authentication"
        )
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: SKIP_AUTH must not be active in this environment",
        )


_MOCK_TEST_USER = {
    "user_id": "c389d596-e7c1-4fd7-900b-f760a0f1c89f",
    "email": "cagurzhan@gmail.com",
    "phone": None,
    "role": "authenticated",
    "is_anonymous": False,
    "app_metadata": {},
    "user_metadata": {},
}


# Use global variables for singletons instead of lru_cache
# This avoids caching issues during reload
_auth_service = None
_initialization_service = None


def get_initialization_service() -> UserInitializationService:
    global _initialization_service
    if _initialization_service is None:
        from src.platform.organization.repository import OrganizationRepository
        from src.platform.profile.repository import ProfileRepositorySupabase
        from src.platform.project.control_plane_dependencies import (
            get_project_control_plane_service,
        )
        from src.platform.project.dependencies import build_project_service
        _initialization_service = UserInitializationService(
            profile_repo=ProfileRepositorySupabase(),
            org_repo=OrganizationRepository(),
            project_service=build_project_service(),
            project_control_plane=get_project_control_plane_service(),
        )
    return _initialization_service


def get_auth_service() -> AuthService:
    """
    Get authentication service instance

    Returns:
        AuthService: Authentication service instance
    """
    global _auth_service
    if _auth_service is None:
        supabase_client = get_supabase_client()
        _auth_service = AuthService(supabase_client)
    return _auth_service


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> CurrentUser:
    """
    Extract and verify JWT token from request headers, return current user info

    This is a FastAPI dependency function for routes that require authentication.

    If SKIP_AUTH=True is configured, authentication is skipped and a mock test user is returned.

    Args:
        credentials: HTTP Bearer authentication credentials
        auth_service: Authentication service instance

    Returns:
        CurrentUser: Currently authenticated user information

    Raises:
        HTTPException: Raises 401 error on authentication failure (only when SKIP_AUTH=False)
    """
    if settings.SKIP_AUTH:
        _assert_skip_auth_safe()
        log_warning("SKIP_AUTH is enabled - returning mock test user")
        return CurrentUser(**_MOCK_TEST_USER)

    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        # Verify token and get user information
        auth_service = get_auth_service()
        current_user = auth_service.get_current_user(token)
        return current_user
    except AuthException as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication failed: {e!s}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> CurrentUser | None:
    """
    Optional user authentication dependency; returns None if no token is provided

    This dependency is for endpoints that can be publicly accessed but also offer user-specific features.

    If SKIP_AUTH=True is configured, a mock test user is always returned.

    Args:
        credentials: HTTP Bearer authentication credentials
        auth_service: Authentication service instance

    Returns:
        Optional[CurrentUser]: Current user info, or None if not authenticated (when SKIP_AUTH=False)
    """
    if settings.SKIP_AUTH:
        _assert_skip_auth_safe()
        log_warning("SKIP_AUTH is enabled - returning mock test user")
        return CurrentUser(**_MOCK_TEST_USER)

    if not credentials:
        return None

    token = credentials.credentials

    try:
        auth_service = get_auth_service()
        current_user = auth_service.get_current_user(token)
        return current_user
    except Exception:
        # For optional auth, return None on failure instead of raising an exception
        return None
