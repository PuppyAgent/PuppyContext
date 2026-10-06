from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.platform.access.adapters.agent.runtime.models import SubmitRun


def test_agent_request_requires_prompt_and_durable_identity():
    with pytest.raises(ValidationError):
        SubmitRun(project_id="project", request_id=uuid4(), prompt="")
    with pytest.raises(ValidationError):
        SubmitRun(project_id="project", prompt="Run this")
    with pytest.raises(ValidationError):
        SubmitRun(
            project_id="project", request_id=uuid4(), prompt="Run this", model="client-selected"
        )
