import pytest

from src.platform.access.adapters.agent.schemas import AgentRequest


def test_agent_request_requires_prompt():
    with pytest.raises(Exception):
        AgentRequest(prompt="")
