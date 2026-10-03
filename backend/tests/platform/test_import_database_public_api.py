"""Database Import boundary with real service/policy and isolated source/provider I/O."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.models import ProjectAction
from src.platform.imports.database import public_router as public
from src.platform.imports.database import service as services
from src.platform.imports.database.dependencies import get_database_import_service
from src.platform.imports.database.models import DBConnection
from tests.authorization_fakes import authorization_for, install_authorization

BASE = "/api/v1/imports/database/sources"
OLD = "/api/v1/db-connector/access"
SECRET = "test-only-source-credential"


class Memory:
    def __init__(self):
        self.rows = {}
        self.touched = []

    def create(self, *, created_by, project_id, name, provider, config):
        now = datetime.now(UTC)
        row = DBConnection(
            id=f"source-{len(self.rows) + 1}",
            created_by=created_by,
            project_id=project_id,
            name=name,
            provider=provider,
            config=config,
            created_at=now,
            updated_at=now,
        )
        self.rows[row.id] = row
        return row

    def get_by_id(self, source_id):
        return self.rows.get(source_id)

    def list_by_project(self, project_id):
        return [row for row in self.rows.values() if row.project_id == project_id]

    def update_last_used(self, source_id):
        self.touched.append(source_id)

    def delete(self, source_id):
        return self.rows.pop(source_id, None) is not None


@pytest.fixture
def environment(monkeypatch):
    memory = Memory()
    authorization = authorization_for("project-1")
    authorization.authorize = Mock(wraps=authorization.authorize)
    service = services.DatabaseImportService(memory, authorization)
    provider = SimpleNamespace(
        test_connection=AsyncMock(return_value={"schema": "public"}),
        list_tables=AsyncMock(
            return_value=[
                SimpleNamespace(
                    name="items", type="table", columns=[{"name": "id", "type": "text"}]
                )
            ]
        ),
        query_table=AsyncMock(
            return_value=SimpleNamespace(
                columns=["id"],
                rows=[{"id": 1, "connection_id": "user-data"}],
                row_count=1,
                execution_time_ms=0.3,
            )
        ),
    )
    monkeypatch.setattr(services, "get_provider", lambda name: provider)
    write = AsyncMock()
    monkeypatch.setattr(
        "src.platform.project.write_lease.build_leased_worker_write_commands",
        lambda: SimpleNamespace(write_bytes=write),
    )
    app = FastAPI()
    app.include_router(public.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-1", role="authenticated"
    )
    app.dependency_overrides[get_database_import_service] = lambda: service
    install_authorization(app, authorization)
    return app, memory, service, provider, write


def create_body():
    return {
        "name": "Source",
        "provider": "supabase",
        "project_url": "https://database.example.test",
        "api_key": SECRET,
        "key_type": "anon",
    }


def test_source_create_reload_preview_save_and_delete_are_one_time_import(environment):
    app, memory, service, provider, write = environment
    with TestClient(app) as client:
        response = client.post(BASE + "?project_id=project-1", json=create_body())
        assert response.status_code == 201, response.text
        assert "connection" not in response.json()["data"] and SECRET not in response.text
        row = response.json()["data"]["source"]
        source_id = row["id"]
        assert row["project_id"] == "project-1" and "config" not in row
        service.authorization.authorize.assert_called_with(
            "project-1", "user-1", ProjectAction.IMPORT_SOURCE_MANAGE
        )
        for url in (BASE + "?project_id=project-1", f"{BASE}/{source_id}"):
            result = client.get(url)
            assert result.status_code == 200 and SECRET not in result.text
        assert client.get(f"{BASE}/{source_id}/tables").json()["data"][0]["name"] == "items"
        preview = client.get(f"{BASE}/{source_id}/tables/items/preview?limit=7")
        assert (
            preview.status_code == 200
            and preview.json()["data"]["rows"][0]["connection_id"] == "user-data"
        )
        provider.query_table.assert_awaited_with(
            memory.rows[source_id].config, table="items", limit=7
        )
        saved = client.post(
            f"{BASE}/{source_id}/save?project_id=project-1",
            json={"name": "items", "table": "items"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"] == {
            "import_database_source_id": source_id,
            "content_path": "items.json",
            "row_count": 1,
        }
        assert write.await_count == 1 and write.await_args.args[:2] == ("project-1", "items.json")
        assert client.delete(f"{BASE}/{source_id}").status_code == 200
        service.authorization.authorize.assert_called_with(
            "project-1", "user-1", ProjectAction.IMPORT_SOURCE_MANAGE
        )
        assert client.get(f"{BASE}/{source_id}").status_code == 404
    assert not memory.rows


@pytest.mark.parametrize("base", [BASE, OLD])
@pytest.mark.parametrize("role", ["viewer", "foreign"])
def test_source_management_policy_is_not_a_sync_or_access_bypass(environment, base, role):
    app, memory, service, provider, write = environment
    row = memory.create(
        created_by="user-1",
        project_id="project-1",
        name="source",
        provider="supabase",
        config={"api_key": SECRET},
    )
    service.authorization = (
        authorization_for("project-1", role=role) if role != "foreign" else authorization_for()
    )
    with TestClient(app) as client:
        assert client.post(base + "?project_id=project-1", json=create_body()).status_code in (
            403,
            404,
        )
        assert client.delete(f"{base}/{row.id}").status_code in (403, 404)
        assert client.post(
            f"{base}/{row.id}/save?project_id=project-1", json={"name": "x", "table": "items"}
        ).status_code in (403, 404)
        if role == "foreign":
            assert client.get(base + "?project_id=project-1").status_code in (403, 404)
            assert client.get(f"{base}/{row.id}/tables").status_code in (403, 404)
    assert set(memory.rows) == {row.id} and memory.touched == []
    provider.test_connection.assert_not_awaited()
    provider.query_table.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.parametrize("source_id", ["access-only", "synchronize-only", "missing"])
def test_another_domain_id_never_becomes_a_database_source(environment, source_id):
    app, _, _, provider, write = environment
    with TestClient(app) as client:
        for suffix in ("", "/tables", "/tables/items/preview"):
            assert client.get(f"{BASE}/{source_id}{suffix}").status_code == 404
        assert client.delete(f"{BASE}/{source_id}").status_code == 404
        assert (
            client.post(
                f"{BASE}/{source_id}/save?project_id=project-1",
                json={"name": "items", "table": "items"},
            ).status_code
            == 404
        )
    provider.query_table.assert_not_awaited()
    write.assert_not_awaited()


def test_mismatched_save_project_is_rejected_before_provider_query_or_content_write(environment):
    app, memory, _, provider, write = environment
    row = memory.create(
        created_by="user-1", project_id="project-1", name="source", provider="supabase", config={}
    )
    with TestClient(app) as client:
        assert (
            client.post(
                f"{BASE}/{row.id}/save?project_id=project-2",
                json={"name": "items", "table": "items"},
            ).status_code
            == 404
        )
    provider.query_table.assert_not_awaited()
    write.assert_not_awaited()
    assert not memory.touched


@pytest.mark.parametrize(
    "query",
    [
        "project_id=",
        "project_id=project-1&project_id=project-2",
        "project_id=project-1&connection_id=source",
        "project_id=project-1&target_folder_path=x",
    ],
)
def test_ambiguous_or_obsolete_source_selectors_fail_before_dispatch(environment, query):
    app, memory, _, provider, _ = environment
    with TestClient(app) as client:
        assert client.get(BASE + "?" + query).status_code == 422
        assert client.post(BASE + "?" + query, json=create_body()).status_code == 422
    assert memory.rows == {}
    provider.test_connection.assert_not_awaited()


def test_unknown_source_body_fields_are_not_silently_dropped(environment):
    app, memory, _, provider, _ = environment
    with TestClient(app) as client:
        for extra in (
            {"target": {"kind": "project_root", "project_id": "project-2"}},
            {"sync_mode": "manual"},
            {"name": " "},
        ):
            assert (
                client.post(
                    BASE + "?project_id=project-1", json={**create_body(), **extra}
                ).status_code
                == 422
            )
    assert memory.rows == {}
    provider.test_connection.assert_not_awaited()
