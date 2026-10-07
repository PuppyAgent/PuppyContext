from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.access.adapters.agent.router import router as agent_router
from src.platform.access.adapters.agent.dependencies import get_agent_service
from src.platform.scope_sandbox.execution.dependencies import get_sandbox_service
from src.platform.access.adapters.agent.chat.dependencies import get_chat_service
from src.tool.dependencies import get_tool_service
from src.infra.s3.dependencies import get_s3_service
from src.platform.access.adapters.agent.config.dependencies import get_agent_config_service
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser


class _DummyAgentService:
    async def stream_events(self, **kwargs):
        if False:
            yield {}


def create_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[get_agent_service] = lambda: _DummyAgentService()
    app.dependency_overrides[get_sandbox_service] = lambda: object()
    app.dependency_overrides[get_chat_service] = lambda: object()
    app.dependency_overrides[get_tool_service] = lambda: object()
    app.dependency_overrides[get_s3_service] = lambda: object()
    app.dependency_overrides[get_agent_config_service] = lambda: object()
    # Keep this validation test authenticated: production intentionally rejects
    # anonymous agent requests before exposing request-shape validation details.
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="test-user",
        email="test@example.com",
        role="authenticated",
    )
    return app


def test_agents_missing_prompt_returns_422():
    app = create_test_app()
    with TestClient(app) as client:
        resp = client.post("/agents/runs", json={})
    assert resp.status_code == 422
