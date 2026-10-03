from types import SimpleNamespace

from src.config import settings
from src.platform.access.public_schemas import AccessSurfaceConfigure
from src.platform.access.router import _create_agent, _create_mcp


def test_unified_mcp_create_returns_one_time_bearer_contract(monkeypatch) -> None:
    issued = "mcp_one_time_secret"

    class Service:
        def __init__(self, repository) -> None:
            self.repository = repository

        def create_endpoint(self, **kwargs):
            return {
                "id": "endpoint-1",
                "project_id": kwargs["project_id"],
                "name": kwargs["name"],
                "status": "active",
                "api_key": issued,
            }

    monkeypatch.setattr(
        "src.platform.access.adapters.mcp_endpoint.repository.McpEndpointRepository",
        lambda: object(),
    )
    monkeypatch.setattr("src.platform.access.adapters.mcp_endpoint.service.McpEndpointService", Service)
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://api.example.test/")

    result = _create_mcp(
        AccessSurfaceConfigure(
            project_id="project-1",
            kind="mcp",
            name="Docs",
        ),
        created_by="user-1",
    )

    assert result.mcp_api_key == issued
    assert result.mcp_server_url == "https://api.example.test/api/v1/mcp/proxy"
    assert issued not in result.mcp_server_url


def test_unified_agent_create_preserves_one_time_mcp_bearer(monkeypatch) -> None:
    issued = "mcp_agent_one_time_secret"

    class Service:
        def __init__(self, repository) -> None:
            self.repository = repository

        def create_agent(self, **kwargs):
            return SimpleNamespace(
                id="agent-1",
                name=kwargs["name"],
                mcp_api_key=issued,
            )

    monkeypatch.setattr(
        "src.platform.access.adapters.agent.config.repository.AgentRepository",
        lambda: object(),
    )
    monkeypatch.setattr("src.platform.access.adapters.agent.config.service.AgentConfigService", Service)
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://api.example.test")

    result = _create_agent(
        AccessSurfaceConfigure(
            project_id="project-1",
            kind="agent",
            name="Assistant",
        )
    )

    assert result.mcp_api_key == issued
    assert result.mcp_server_url == "https://api.example.test/api/v1/mcp/proxy"
