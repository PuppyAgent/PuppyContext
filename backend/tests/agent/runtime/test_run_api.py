"""Real control-plane storage behind production FastAPI routes and admission."""

import concurrent.futures
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.exceptions import PermissionException
from src.platform.access.adapters.agent.dependencies import get_agent_service
from src.platform.access.adapters.agent.router import router
from src.platform.access.adapters.agent.runtime.models import SubmitRun
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture

prepared = prepared_fixture
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_run_api_replay_snapshot_and_cross_account_denial(prepared):
    case = prepared
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: case.service
    identity = CurrentUser(user_id=case.user, email="fixture@example.com", role="authenticated")
    app.dependency_overrides[get_current_user] = lambda: identity
    run_id = case.run["id"]
    with TestClient(app) as client:
        snapshot = client.get("/agents/runs/" + run_id)
        assert snapshot.status_code == 200
        request = snapshot.json()["request_id"]
        assert client.get(f"/agents/requests/{case.project}/{request}").json()["id"] == run_id
        assert (
            client.get(f"/agents/sessions/{case.run['session_id']}/runs").json()[0]["id"] == run_id
        )
        assert (
            client.get(
                f"/agents/runs/{run_id}/events", headers={"Last-Event-ID": "invalid"}
            ).status_code
            == 400
        )
        case.postgres.sql(
            f"SELECT agent_run_event('{run_id}', 'text', '{{}}') FROM generate_series(1,520)"
        )
        response = client.get(f"/agents/runs/{run_id}/events")
        assert "event: reset" in response.text
        reset = json.loads(response.text.split("data: ")[1].strip())
        assert reset["id"] == run_id and reset["sequence"] > 512
        identity = CurrentUser(
            user_id=str(uuid4()), email="other@example.com", role="authenticated"
        )
        for method, path in [("get", ""), ("post", "/stop"), ("get", "/events")]:
            assert getattr(client, method)(f"/agents/runs/{run_id}" + path).status_code == 404
        assert client.get(f"/agents/sessions/{case.run['session_id']}/runs").status_code == 404


@pytest.mark.asyncio
async def test_builtin_admission_preserves_config_and_limits_creation(prepared, monkeypatch):
    case = prepared
    case.postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE project_id='{case.project}'")
    before = case.admission.surface(case.agent)
    request = SubmitRun(project_id=case.project, request_id=str(uuid4()), prompt="first submit")
    first = case.service.submit(case.user, request)
    assert first["agent_id"] == case.agent
    assert case.admission.surface(case.agent)["config"] == before["config"]
    assert case.service.submit(case.user, request)["id"] == first["id"]
    case.postgres.sql(
        f"UPDATE agent_runs SET state='failed' WHERE project_id='{case.project}'; DELETE FROM access_surfaces WHERE id='{case.agent}'"
    )
    editor = str(uuid4())
    org = case.postgres.row(f"SELECT org_id FROM projects WHERE id='{case.project}'")["org_id"]
    case.postgres.sql(
        f"INSERT INTO auth.users(id) VALUES('{editor}'); INSERT INTO org_members(org_id,user_id,role) VALUES('{org}','{editor}','member'); INSERT INTO project_members(project_id,org_id,user_id,role) VALUES('{case.project}','{org}','{editor}','editor')"
    )
    with pytest.raises(PermissionException) as error:
        case.service.submit(editor, request.model_copy(update={"request_id": uuid4()}))
    assert error.value.status_code == 403
    assert (
        case.postgres.sql(
            f"SELECT count(*) FROM access_surfaces WHERE project_id='{case.project}' AND kind='agent'"
        )
        == "0"
    )
    monkeypatch.setattr(settings, "CLOUD_AGENT_DEFAULT_MODEL", "fixture")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: case.service.submit(case.user, request), range(6)))
    assert len({run["id"] for run in results}) == 1
    assert (
        case.postgres.sql(
            f"SELECT count(*) FROM access_surfaces WHERE project_id='{case.project}' AND kind='agent'"
        )
        == "1"
    )
    assert not case.repo.executions(results[0]["id"])
