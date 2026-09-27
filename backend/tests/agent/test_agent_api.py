from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.access.adapters.agent.router import router as agent_router
from src.platform.access.adapters.agent.dependencies import get_agent_service
from src.platform.scope_sandbox.execution.dependencies import get_sandbox_service
from src.platform.access.adapters.agent.chat.dependencies import get_chat_service
from src.tool.dependencies import get_tool_service
from src.infra.s3.dependencies import get_s3_service
from src.platform.access.adapters.agent.config.dependencies import get_agent_config_service


class _DummyAgentService:
    async def stream_events(self, **kwargs):
        yield {"type": "text", "content": "ok"}


def create_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[get_agent_service] = lambda: _DummyAgentService()
    app.dependency_overrides[get_sandbox_service] = lambda: object()
    app.dependency_overrides[get_chat_service] = lambda: object()
    app.dependency_overrides[get_tool_service] = lambda: object()
    app.dependency_overrides[get_s3_service] = lambda: object()
    app.dependency_overrides[get_agent_config_service] = lambda: object()
    return app


def test_agents_route_not_found():
    app = create_test_app()
    with TestClient(app) as client:
        resp = client.post("/agents", json={"prompt": "hi"})
    assert resp.status_code != 404
