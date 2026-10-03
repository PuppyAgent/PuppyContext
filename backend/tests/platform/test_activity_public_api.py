"""Read-only canonical Activity kind and unchanged Project authorization."""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.activity.dependencies import get_activity_service
from src.platform.activity.public_router import router
from src.platform.activity.schemas import ActivityItemResponse
from src.platform.activity.service import ActivityService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.models import (
    ACTION_CAPABILITY,
    ROLE_CAPABILITIES,
    ProjectAction,
    ProjectCapability,
    ProjectRole,
)
from tests.authorization_fakes import authorization_for, install_authorization


@pytest.fixture
def environment():
    rows = [
        ActivityItemResponse(
            id="same-id",
            project_id="project-1",
            kind=kind,
            message="historical sync_run connection_id text",
        )
        for kind in ("upload", "import", "synchronize_run")
    ]
    repo = Mock()
    repo.list_by_project.return_value = rows
    service = ActivityService(
        repo=repo, authorization=authorization_for("project-1", role="viewer")
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    install_authorization(app, service.authorization)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-1", role="authenticated"
    )
    app.dependency_overrides[get_activity_service] = lambda: service
    return app, repo, service


def test_canonical_activity_is_typed_and_does_not_rewrite_historical_identity_or_text(environment):
    app, repo, _ = environment
    with TestClient(app) as client:
        result = client.get("/api/v1/activity/items?project_id=project-1")
        assert result.status_code == 200, result.text
        rows = result.json()["data"]["items"]
        assert [row["kind"] for row in rows] == ["upload", "import", "synchronize_run"]
        assert all(
            row["id"] == "same-id" and row["message"] == "historical sync_run connection_id text"
            for row in rows
        )
        repo.list_by_project.assert_called_once_with(
            "project-1", kind=None, active_only=False, limit=50
        )
        repo.reset_mock()
        repo.list_by_project.return_value = []
        assert (
            client.get(
                "/api/v1/activity/items?project_id=project-1&kind=synchronize_run&active_only=true&limit=7"
            ).status_code
            == 200
        )
        repo.list_by_project.assert_called_once_with(
            "project-1", kind="synchronize_run", active_only=True, limit=7
        )
        assert client.post("/api/v1/activity/items", json={}).status_code == 405


@pytest.mark.parametrize(
    "query",
    [
        "project_id=",
        "project_id=project-1&kind=sync_run",
        "project_id=project-1&kind=access",
        "project_id=project-1&project_id=other",
        "project_id=project-1&connection_id=x",
        "project_id=project-1&kind=",
        "project_id=project-1&limit=0",
    ],
)
def test_invalid_activity_selectors_cannot_broaden_inventory(environment, query):
    app, repo, _ = environment
    with TestClient(app) as client:
        assert client.get("/api/v1/activity/items?" + query).status_code == 422
    repo.list_by_project.assert_not_called()


def test_foreign_project_activity_is_denied_before_repository(environment):
    app, repo, service = environment
    service.authorization = authorization_for()
    with TestClient(app) as client:
        assert client.get("/api/v1/activity/items?project_id=project-1").status_code in (403, 404)
    repo.list_by_project.assert_not_called()


def test_synchronize_permission_wire_changes_without_role_escalation_or_alias():
    assert (
        ProjectAction.SYNCHRONIZE_MANAGE.value
        == ProjectCapability.SYNCHRONIZE_MANAGE.value
        == "synchronize.manage"
    )
    assert (
        ACTION_CAPABILITY[ProjectAction.SYNCHRONIZE_MANAGE] is ProjectCapability.SYNCHRONIZE_MANAGE
    )
    for role in ProjectRole:
        assert (ProjectCapability.SYNCHRONIZE_MANAGE in ROLE_CAPABILITIES[role]) == (
            role == ProjectRole.ADMIN
        )
    for enum in (ProjectAction, ProjectCapability):
        with pytest.raises(ValueError):
            enum("integration.manage")
