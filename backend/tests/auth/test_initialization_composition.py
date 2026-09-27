"""Exercise real service wiring: endpoint mocks cannot detect Depends placeholders."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.platform.auth import dependencies as auth
from src.platform.organization import repository as organizations
from src.platform.profile import dependencies as profile
from src.platform.profile import repository as profiles
from src.platform.project import control_plane_dependencies as control_plane
from src.platform.project import dependencies as projects


async def test_imperative_auth_and_profile_factories_resolve_real_project_services(monkeypatch):
    repository = MagicMock()
    repository.get_by_org_id.return_value = [{"id": "existing-project"}]
    profile_repository = MagicMock()
    profile_repository.get_by_user_id.return_value = SimpleNamespace(has_onboarded=False)
    monkeypatch.setattr(projects, "_project_repository", repository)
    monkeypatch.setattr(projects, "_project_membership_repository", MagicMock())
    monkeypatch.setattr(auth, "_initialization_service", None)
    monkeypatch.setattr(profiles, "ProfileRepositorySupabase", lambda: profile_repository)
    monkeypatch.setattr(organizations, "OrganizationRepository", MagicMock())
    monkeypatch.setattr(control_plane, "get_project_control_plane_service", MagicMock())
    monkeypatch.setattr(profile, "get_profile_repository", lambda: profile_repository)

    initialization = auth.get_initialization_service()
    assert await initialization.maybe_seed_demo_project("user", "organization") is None
    repository.get_by_org_id.assert_called_once_with("organization")
    profile_repository.mark_onboarded.assert_called_once_with(user_id="user")
    assert profile.get_profile_service()._project_service.get_by_org_id("organization") == [
        {"id": "existing-project"}
    ]


def test_http_project_dependency_honors_repository_overrides_after_imperative_use(monkeypatch):
    background_repository = MagicMock()
    monkeypatch.setattr(projects, "_project_repository", background_repository)
    monkeypatch.setattr(projects, "_project_membership_repository", MagicMock())
    background_service = projects.build_project_service()
    request_repository = MagicMock()
    request_repository.get_by_org_id.return_value = [{"id": "request-project"}]
    app = FastAPI()
    app.dependency_overrides[projects.get_project_repository] = lambda: request_repository
    app.dependency_overrides[projects.get_project_membership_repository] = lambda: MagicMock()

    @app.get("/projects")
    def read_projects(service=Depends(projects.get_project_service)):
        return service.get_by_org_id("organization")

    with TestClient(app) as client:
        response = client.get("/projects")
    assert response.status_code == 200
    assert response.json() == [{"id": "request-project"}]
    assert background_service.repo is background_repository
    background_repository.get_by_org_id.assert_not_called()
