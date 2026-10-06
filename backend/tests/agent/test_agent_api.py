from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.access.adapters.agent.router import router


def test_legacy_execution_route_is_retired(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "SKIP_AUTH", False)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post("/agents", json={"prompt": "hi"}).status_code == 404
        assert client.post("/agents/runs", json={"prompt": "hi"}).status_code == 401
