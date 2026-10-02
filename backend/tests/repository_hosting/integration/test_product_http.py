import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.bootstrap.dependencies import get_version_write_command_service
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.entrypoints.http.content_write import write_router
from src.version_engine.write_engine.tree_objects import flatten_tree_to_bytes

pytestmark = pytest.mark.hosting_component


@pytest.fixture
def product_http(component_repo, monkeypatch):
    state = component_repo

    def write_state(project_id, user_id):
        return ProjectWriteState(
            project_id=project_id,
            project_name="Test",
            role="editor",
            can_write=True,
            root_hash=state.repo.history.get_root_hash(),
            head_commit_id=state.repo.history.get_head_commit_id(),
        )

    monkeypatch.setattr(state.adapter, "get_project_write_state", write_state)
    app = FastAPI()
    app.include_router(write_router, prefix="/api/v1/content")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="test-user", role="authenticated"
    )
    app.dependency_overrides[get_version_write_command_service] = lambda: (
        VersionWriteCommandService(state.adapter)
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, state


def files(state):
    return flatten_tree_to_bytes(state.repo.store, state.repo.history.get_root_hash())


@pytest.mark.parametrize("content", ["", "中文\n", "one\r\ntwo\n", "\u0000binary"])
def test_http_write_persists_exact_bytes(product_http, content):
    client, state = product_http
    response = client.post(
        "/api/v1/content/test-proj/write",
        json={"path": "f.txt", "content": content, "node_type": "file"},
    )
    assert response.status_code == 200, response.text
    oid = response.json()["data"]["commit_id"]
    assert oid == state.repo.history.get_head_commit_id()
    assert files(state)["f.txt"] == content.encode()


def test_stale_web_base_returns_409_and_preserves_saved_data(product_http):
    client, state = product_http
    path = "/api/v1/content/test-proj/write"
    first = client.post(path, json={"path": "f.txt", "content": "first", "node_type": "file"})
    base = first.json()["data"]["commit_id"]
    saved = client.post(
        path,
        json={"path": "f.txt", "content": "saved", "node_type": "file", "base_commit_id": base},
    )
    assert saved.status_code == 200, saved.text
    head = state.repo.history.get_head_commit_id()
    stale = client.post(
        path,
        json={"path": "f.txt", "content": "stale", "node_type": "file", "base_commit_id": base},
    )
    assert stale.status_code == 409, stale.text
    assert state.repo.history.get_head_commit_id() == head
    assert files(state)["f.txt"] == b"saved"


def test_viewer_is_denied_before_any_file_mutation(product_http, monkeypatch):
    client, state = product_http
    monkeypatch.setattr(
        state.adapter,
        "get_project_write_state",
        lambda *a, **k: ProjectWriteState("test-proj", "Test", role="viewer", can_write=False),
    )
    response = client.post(
        "/api/v1/content/test-proj/write", json={"path": "f", "content": "forbidden"}
    )
    assert response.status_code == 403
    assert files(state) == {}


def test_bulk_request_invalid_member_does_not_write_valid_member(product_http):
    client, state = product_http
    response = client.post(
        "/api/v1/content/test-proj/bulk-write",
        json={
            "files": [
                {"path": "good.txt", "content": "keep", "node_type": "file"},
                {"path": "../escape", "content": "bad", "node_type": "file"},
            ]
        },
    )
    assert 400 <= response.status_code < 500, response.text
    assert files(state) == {}
