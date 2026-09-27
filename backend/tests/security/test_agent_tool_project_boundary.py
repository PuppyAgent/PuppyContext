from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.platform.access.adapters.agent.config.models import Agent
from src.platform.access.adapters.agent.mcp.service import McpV3Service
from src.exceptions import NotFoundException
from src.tool.models import Tool


def _agent() -> Agent:
    now = datetime.now(UTC)
    return Agent(
        id="agent-1",
        project_id="project-1",
        name="Agent",
        created_at=now,
        updated_at=now,
    )


def _tool(
    tool_id: str,
    *,
    project_id: str | None,
    org_id: str = "org-1",
) -> Tool:
    return Tool(
        id=tool_id,
        created_at=datetime.now(UTC),
        org_id=org_id,
        project_id=project_id,
        type="search",
        name=tool_id,
    )


class AgentRepo:
    def __init__(self):
        self.agent = _agent()
        self.upserts = []

    def get_by_id(self, agent_id):
        return self.agent if agent_id == self.agent.id else None

    def is_visible_to(self, _agent_id, _user_id):
        return True

    def get_project_org_id(self, _project_id):
        return "org-1"

    def upsert_tool_binding(self, **kwargs):
        self.upserts.append(kwargs)
        return SimpleNamespace(**kwargs)


class ToolRepo:
    def __init__(self, tools):
        self.tools = {tool.id: tool for tool in tools}

    def get_by_id(self, tool_id):
        return self.tools.get(tool_id)


def test_agent_cannot_bind_a_tool_from_a_sibling_project():
    agents = AgentRepo()
    service = McpV3Service(
        agent_repo=agents,
        tool_repo=ToolRepo([_tool("tool-other", project_id="project-2")]),
    )
    with pytest.raises(NotFoundException):
        service.bind_tool("agent-1", "user-1", "tool-other")
    assert agents.upserts == []


def test_agent_can_bind_same_project_and_same_org_shared_tools():
    agents = AgentRepo()
    service = McpV3Service(
        agent_repo=agents,
        tool_repo=ToolRepo(
            [
                _tool("tool-project", project_id="project-1"),
                _tool("tool-org", project_id=None),
            ]
        ),
    )
    service.bind_tool("agent-1", "user-1", "tool-project")
    service.bind_tool("agent-1", "user-1", "tool-org")
    assert [row["tool_id"] for row in agents.upserts] == [
        "tool-project",
        "tool-org",
    ]


def test_agent_cannot_bind_an_org_tool_from_another_tenant():
    agents = AgentRepo()
    service = McpV3Service(
        agent_repo=agents,
        tool_repo=ToolRepo(
            [_tool("tool-foreign-org", project_id=None, org_id="org-2")]
        ),
    )
    with pytest.raises(NotFoundException):
        service.bind_tool("agent-1", "user-1", "tool-foreign-org")
    assert agents.upserts == []
