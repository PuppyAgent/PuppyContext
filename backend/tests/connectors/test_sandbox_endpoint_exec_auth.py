from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.access.adapters.sandbox_endpoint.dependencies import get_sandbox_endpoint_service
from src.platform.access.adapters.sandbox_endpoint.router import router
from src.platform.scope_sandbox.execution.dependencies import get_sandbox_service


class _SandboxService:
    pass


class _InvalidCredentialService:
    def get_by_access_key(self, _access_key: str):
        return None


class _UnavailableCredentialService:
    def get_by_access_key(self, _access_key: str):
        raise RuntimeError("credential store unavailable")


def _client(service) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_sandbox_endpoint_service] = lambda: service
    app.dependency_overrides[get_sandbox_service] = lambda: _SandboxService()
    return TestClient(app)


def test_exec_rejects_unknown_sandbox_credential_before_execution():
    with _client(_InvalidCredentialService()) as client:
        response = client.post(
            "/sandbox-endpoints/endpoint-1/exec",
            headers={"X-Access-Key": "sbx_invalid"},
            json={"command": "echo should-not-run"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid access key"


def test_exec_fails_closed_when_credential_store_is_unavailable():
    with _client(_UnavailableCredentialService()) as client:
        response = client.post(
            "/sandbox-endpoints/endpoint-1/exec",
            headers={"X-Access-Key": "sbx_invalid"},
            json={"command": "echo should-not-run"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Sandbox credential validation is temporarily unavailable"
