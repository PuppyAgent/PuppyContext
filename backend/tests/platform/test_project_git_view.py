from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.project.dependencies import get_project_repository
from src.platform.project.git_view import ProjectGitViewService
from src.platform.project.router import (
    get_project_git_view_service,
)
from src.platform.project.router import (
    router as project_router,
)
from src.platform.repository_target.protocol import require_repository_target_contract
from tests.authorization_fakes import authorization_for, install_authorization

PROJECT_ID = "project-1"


def test_health_and_rebuild_bind_the_native_endpoint_to_the_human_grant(monkeypatch):
    from src.platform.project import git_view as module

    manager = MagicMock()
    endpoint = MagicMock()
    endpoint.health.return_value = {"health": "healthy"}
    endpoint.rebuild.return_value = {"required": False}
    factory = MagicMock(return_value=endpoint)
    monkeypatch.setattr(module, "NativeGitEndpoint", factory)
    grant = object()
    service = ProjectGitViewService(manager)
    assert service.health(
        PROJECT_ID, grant=grant, content_write_allowed=False, cache_rebuild_allowed=True
    ) == {"health": "healthy", "can_rebuild": True}
    factory.assert_called_once_with(
        manager.get_native_service.return_value, grant, manager.get_audit.return_value
    )
    assert service.rebuild(PROJECT_ID, grant=grant) == {"required": False}
    endpoint.rebuild.assert_called_once_with()


def test_unmigrated_repository_has_no_old_view_fallback():
    manager = MagicMock()
    manager.get_native_service.return_value = None
    with pytest.raises(RuntimeError, match="migration required"):
        ProjectGitViewService(manager).health(
            PROJECT_ID, grant=object(), content_write_allowed=False, cache_rebuild_allowed=False
        )
    manager.get_server_repo.assert_not_called()


def _app(role: str):
    app = FastAPI()
    app.include_router(project_router, prefix="/api/v1")
    app.dependency_overrides[require_repository_target_contract] = lambda: 2
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-1",
        email="user@example.com",
        role="authenticated",
    )
    install_authorization(app, authorization_for(PROJECT_ID, role=role))

    project_repository = MagicMock()
    project_repository.get_by_id.return_value = SimpleNamespace(id=PROJECT_ID)
    app.dependency_overrides[get_project_repository] = lambda: project_repository

    git_view = MagicMock()
    git_view.health.return_value = {"health": "healthy", "can_rebuild": role == "admin"}
    git_view.rebuild.return_value = {"variants": []}
    app.dependency_overrides[get_project_git_view_service] = lambda: git_view
    return app, git_view


@pytest.mark.parametrize(
    ("role", "content_write", "can_rebuild"),
    [("viewer", False, False), ("editor", True, False), ("admin", True, True)],
)
def test_health_control_plane_uses_project_read_and_passes_capabilities(
    role,
    content_write,
    can_rebuild,
):
    app, git_view = _app(role)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/projects/{PROJECT_ID}/git-view/health")

    assert response.status_code == 200, response.text
    git_view.health.assert_called_once_with(
        PROJECT_ID,
        grant=ANY,
        content_write_allowed=content_write,
        cache_rebuild_allowed=can_rebuild,
    )


def test_cache_rebuild_control_plane_requires_project_management():
    viewer_app, viewer_service = _app("viewer")
    with TestClient(viewer_app) as client:
        denied = client.post(f"/api/v1/projects/{PROJECT_ID}/git-view/rebuild-cache")
    assert denied.status_code == 403, denied.text
    viewer_service.rebuild.assert_not_called()

    admin_app, admin_service = _app("admin")
    with TestClient(admin_app) as client:
        allowed = client.post(f"/api/v1/projects/{PROJECT_ID}/git-view/rebuild-cache")
    assert allowed.status_code == 200, allowed.text
    admin_service.rebuild.assert_called_once_with(PROJECT_ID, grant=ANY)
