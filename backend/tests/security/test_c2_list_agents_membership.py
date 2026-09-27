"""Agent routes consume the canonical action-based Project PDP."""

from __future__ import annotations

from unittest.mock import MagicMock
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.platform.access.adapters.agent.config.dependencies import get_agent_config_service
from src.platform.access.adapters.agent.config.models import Agent
from src.platform.access.adapters.agent.config.router import router as agent_config_router
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.version_engine.bootstrap.dependencies import get_product_operation_adapter


ALLOWED_PROJECT = "proj-A"
DENIED_PROJECT = "proj-B"


class FakeAuthorization:
    def __init__(self):
        self.calls: list[tuple[str, str, ProjectAction]] = []

    def authorize(self, project_id, user_id, action, **_kwargs):
        self.calls.append((project_id, user_id, action))
        if project_id != ALLOWED_PROJECT:
            raise HTTPException(status_code=403, detail="Project access denied")
        return MagicMock()


def _make_app():
    app = FastAPI()
    app.include_router(agent_config_router, prefix="/api/v1")
    user = CurrentUser(
        user_id="user-alice", email="a@example.com", role="authenticated"
    )
    authorization = FakeAuthorization()
    agents = MagicMock()
    agents.list_agents.return_value = []
    agents.get_default_agent.return_value = None
    now = datetime.now(UTC)
    agents.create_agent.return_value = Agent(
        id="agent-1",
        project_id=ALLOWED_PROJECT,
        name="Agent",
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_authorization_service] = lambda: authorization
    app.dependency_overrides[get_agent_config_service] = lambda: agents
    app.dependency_overrides[get_product_operation_adapter] = lambda: MagicMock()
    return app, authorization, agents


def test_list_agents_authorizes_agent_read_before_query():
    app, authorization, agents = _make_app()
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/agent-config/?project_id={ALLOWED_PROJECT}"
        )
    assert response.status_code == 200
    assert authorization.calls == [
        (ALLOWED_PROJECT, "user-alice", ProjectAction.AGENT_READ)
    ]
    agents.list_agents.assert_called_once()


def test_list_agents_denial_prevents_data_read():
    app, _authorization, agents = _make_app()
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/agent-config/?project_id={DENIED_PROJECT}"
        )
    assert response.status_code == 403
    agents.list_agents.assert_not_called()


def test_default_agent_denial_prevents_data_read():
    app, _authorization, agents = _make_app()
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/agent-config/default?project_id={DENIED_PROJECT}"
        )
    assert response.status_code == 403
    agents.get_default_agent.assert_not_called()


def test_create_agent_requires_agent_manage():
    app, authorization, agents = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agent-config/",
            json={"project_id": ALLOWED_PROJECT, "name": "Agent"},
        )
    assert response.status_code != 403
    assert authorization.calls[0] == (
        ALLOWED_PROJECT,
        "user-alice",
        ProjectAction.AGENT_MANAGE,
    )


def test_create_agent_denial_prevents_mutation():
    app, _authorization, agents = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agent-config/",
            json={"project_id": DENIED_PROJECT, "name": "Evil Agent"},
        )
    assert response.status_code == 403
    agents.create_agent.assert_not_called()
