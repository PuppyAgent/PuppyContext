import pytest

from src.platform.access.adapters.agent.runtime.repository import RunRepository

pytestmark = pytest.mark.integration


def test_production_repository_uses_real_postgrest(control_api, submitted):
    args, row = submitted
    repository = RunRepository(control_api)
    assert repository.receipt(args["user"], args["project"], args["request"])["id"] == row["id"]
    claim = repository.rpc("claim", worker="real-postgrest")
    assert claim["id"] == row["id"]
    current = repository.write(claim, "state", {"state": "running"}, state="running")
    assert current["state"] == "running"
    result = repository.tool(
        current, {"call_id": "read_1", "name": "read", "input": {"path": "file.txt"}}, "executing"
    )
    assert result["state"] == "executing"
    assert repository.tools(row["id"])[0]["call_id"] == "read_1"
