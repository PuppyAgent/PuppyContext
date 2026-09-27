"""
Project Dependency Injection
"""

from typing import Annotated

from fastapi import Depends

from src.platform.authorization.repository import ProjectMembershipRepository
from src.platform.project.repository import ProjectRepositorySupabase
from src.platform.project.service import ProjectService

# Use global variables for singletons instead of creating new instances each time
# This avoids redundant initialization and improves performance
_project_repository = None
_project_membership_repository = None


def get_project_repository() -> ProjectRepositorySupabase:
    """
    Get project repository singleton

    Returns:
        ProjectRepositorySupabase instance
    """
    global _project_repository
    if _project_repository is None:
        _project_repository = ProjectRepositorySupabase()
    return _project_repository


def get_project_membership_repository() -> ProjectMembershipRepository:
    global _project_membership_repository
    if _project_membership_repository is None:
        _project_membership_repository = ProjectMembershipRepository()
    return _project_membership_repository


def build_project_service() -> ProjectService:
    """Compose the service for callers outside FastAPI's dependency resolver."""
    return ProjectService(get_project_repository(), get_project_membership_repository())


def get_project_service(
    repository: Annotated[ProjectRepositorySupabase, Depends(get_project_repository)],
    membership_repository: Annotated[
        ProjectMembershipRepository, Depends(get_project_membership_repository)
    ],
) -> ProjectService:
    """Request-scoped service; FastAPI resolves and caches its dependencies.

    Required arguments prevent imperative callers from accidentally storing
    unresolved Depends placeholders in a process-wide service singleton.
    """
    return ProjectService(repository, membership_repository)
