"""ISSUE-059: real application boundaries, isolated persistence/queue doubles.

These tests do not claim production data migration or real Provider execution.
"""
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.exceptions import BusinessException
from src.platform.access.service import AccessService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.repository_target.models import ProjectRootTarget
from src.platform.synchronize import router as sync_router
from src.platform.synchronize.dependencies import get_synchronize_service
from src.platform.synchronize.repository import SourceConnection
from src.platform.synchronize.schemas import connection_to_response
from tests.authorization_fakes import authorization_for


@pytest.mark.parametrize("kind", ["notion", "gmail", "url", "database", "unknown_source"])
def test_access_rejects_external_sources_before_persistence(kind):
    repo = Mock()
    service = AccessService(repository=repo, surface_repository=Mock(), scope_repository=Mock())
    with pytest.raises(BusinessException, match="Synchronize"):
        service.create(
            project_id="project-1", target=ProjectRootTarget(project_id="project-1"),
            kind=kind, direction="inbound", name="Same name", config={}, policy={},
            oauth_connection_id=123, trigger={"type": "manual"}, created_by="user-1",
        )
    repo.insert.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_access_run_cannot_dispatch_surface_id_as_binding_id(monkeypatch):
    from src.platform.synchronize import dependencies

    engine_factory = Mock()
    monkeypatch.setattr(dependencies, "create_synchronize_engine", engine_factory)
    repo = Mock()
    repo.get.return_value = SimpleNamespace(
        id="access-id", kind="gmail", is_builtin=False, status="active",
    )
    service = AccessService(repository=repo, surface_repository=Mock(), scope_repository=Mock())
    with pytest.raises(BusinessException, match="Synchronize"):
        await service.run_now("access-id")
    engine_factory.assert_not_called()
    repo.update.assert_not_called()


def test_binding_response_preserves_management_state_and_redacts_secrets():
    binding = SourceConnection(
        id="binding-id", project_id="project-1", provider="url", path="not-a-scope",
        trigger={"type": "scheduled", "schedule": "0 9 * * *", "timezone": "UTC"},
        config={"source_url": "https://example.test", "credentials_ref": "secret", "access_key": "secret"},
        last_synced_at="2026-10-02T10:00:00Z", created_at="2026-10-01T10:00:00Z",
    )
    before = asdict(binding)
    response = connection_to_response(binding)
    assert response["trigger"] == binding.trigger
    assert response["last_synced_at"] == binding.last_synced_at
    assert response["created_at"] == binding.created_at
    assert response["config"] == {"source_url": "https://example.test"}
    assert asdict(binding) == before


def test_binding_list_uses_binding_repository_and_keeps_different_resource_ids():
    repo = Mock()
    repo.list_by_project.return_value = [SourceConnection(
        id="binding-id", project_id="project-1", provider="url", path="not-a-scope",
        config={"source_url": "https://example.test"}, trigger={"type": "scheduled"},
    )]
    app = FastAPI()
    app.include_router(sync_router.router, prefix="/api/v1")
    app.dependency_overrides[get_synchronize_service] = lambda: SimpleNamespace(repository=repo)
    app.dependency_overrides[get_authorization_service] = lambda: authorization_for("project-1", role="admin")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="user-1", role="authenticated")
    with TestClient(app) as client:
        response = client.get("/api/v1/integrations/connections", params={"project_id": "project-1"})
    assert response.status_code == 200
    rows = response.json()["data"]
    assert [row["id"] for row in rows] == ["binding-id"]
    assert rows[0]["trigger"] == {"type": "scheduled"}
    assert rows[0]["path"] == "not-a-scope"
    assert "access_point_id" not in rows[0]
    repo.list_by_project.assert_called_once_with("project-1")
