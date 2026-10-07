"""src.internal.router ProductOperationAdapter-based internal endpoint tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.internal.router import router as internal_router
from src.internal.router import verify_internal_secret
from src.version_engine.bootstrap.dependencies import get_product_operation_adapter
from tests.authorization_fakes import authorization_for


@dataclass
class FakeVersionEntry:
    name: str
    path: str
    type: str
    content_hash: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    children_count: int | None = None


class FakeProductOperationAdapter:
    """Mock ProductOperationAdapter for testing internal router endpoints."""

    def __init__(self):
        self._stat_map: dict[str, FakeVersionEntry | None] = {}
        self._list_dir_map: dict[str, list[FakeVersionEntry]] = {}
        self._read_file_map: dict[str, bytes] = {}
        self.write_file = AsyncMock()
        self.mkdir = AsyncMock()
        self.move = AsyncMock()
        self.delete = AsyncMock()

    def for_user(self, project_id: str, user_id: str):
        assert (project_id, user_id) == ("proj-1", "test-user")
        return self

    def stat(self, project_id: str, path: str) -> FakeVersionEntry | None:
        return self._stat_map.get(path)

    def list_dir(self, project_id: str, path: str = "") -> list[FakeVersionEntry]:
        return self._list_dir_map.get(path, [])

    def read_file(self, project_id: str, path: str) -> bytes:
        if path not in self._read_file_map:
            raise FileNotFoundError(path)
        return self._read_file_map[path]


class FakeWriteCommands:
    def __init__(self, ops: FakeProductOperationAdapter):
        self.ops = ops

    async def write_file(self, project_id: str, path: str, content, **kwargs):
        result = await self.ops.write_file(project_id, path, content, **kwargs)
        return SimpleNamespace(path=path, result=result)

    async def mkdir(self, project_id: str, path: str, **kwargs):
        result = await self.ops.mkdir(project_id, path, **kwargs)
        return SimpleNamespace(path=path, result=result)

    async def delete(self, project_id: str, paths: list[str], **kwargs):
        actor = kwargs.pop("actor", "mcp_agent")
        result = await self.ops.delete(project_id, paths, who=actor, **kwargs)
        return SimpleNamespace(path=paths[0], result=result)


@pytest.fixture
def ops():
    return FakeProductOperationAdapter()


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.include_router(internal_router)
    return test_app


@pytest.fixture
def client(app, ops):
    app.dependency_overrides[verify_internal_secret] = lambda: None
    app.dependency_overrides[get_product_operation_adapter] = lambda: ops
    # SECURITY (C-3): all /internal/nodes/* endpoints now require an
    # X-Acting-User-Id header AND verify the user has project access.
    # The pre-existing tests don't set up real users, so we patch the
    # access check to always allow.
    with (
        patch(
            "src.platform.authorization.factory.build_authorization_service",
            return_value=authorization_for("proj-1"),
        ),
        TestClient(app) as c,
    ):
        # Inject the required header for every request via headers=
        c.headers.update({"X-Acting-User-Id": "test-user"})
        yield c
    app.dependency_overrides.clear()


def test_resolve_node_path_success(client, ops):
    ops._stat_map["docs/readme.md"] = FakeVersionEntry(
        name="readme.md",
        path="docs/readme.md",
        type="markdown",
        size_bytes=42,
    )

    resp = client.post(
        "/internal/nodes/resolve-path",
        json={
            "project_id": "proj-1",
            "root_accesses": [],
            "path": "/docs/readme.md",
        },
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "readme.md"
    assert body["type"] == "markdown"
    assert body["path"] == "docs/readme.md"


def test_resolve_node_path_virtual_root(client, ops):
    resp = client.post(
        "/internal/nodes/resolve-path",
        json={"project_id": "proj-1", "root_accesses": [], "path": "/"},
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"virtual_root": True, "path": "/"}


def test_list_node_children_returns_dot_entries(client, ops):
    ops._list_dir_map["docs"] = [
        FakeVersionEntry(name=".internal", path="docs/.internal", type="folder", children_count=0),
        FakeVersionEntry(name="readme.md", path="docs/readme.md", type="markdown", size_bytes=42),
    ]

    resp = client.get(
        "/internal/nodes/list?project_id=proj-1&path=docs",
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "docs"
    names = [c["name"] for c in body["children"]]
    assert ".internal" in names
    assert "readme.md" in names


def test_read_node_content_json_returns_content(client, ops):
    ops._stat_map["users.json"] = FakeVersionEntry(
        name="users.json",
        path="users.json",
        type="json",
        size_bytes=20,
    )
    ops._read_file_map["users.json"] = b'{"users": []}'

    resp = client.get(
        "/internal/nodes/read?project_id=proj-1&path=users.json",
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "json"
    assert body["content"] == {"users": []}


def test_read_node_content_folder_returns_dot_children(client, ops):
    ops._stat_map["docs"] = FakeVersionEntry(
        name="docs",
        path="docs",
        type="folder",
    )
    ops._list_dir_map["docs"] = [
        FakeVersionEntry(name=".internal", path="docs/.internal", type="folder"),
        FakeVersionEntry(name="guide.md", path="docs/guide.md", type="markdown", size_bytes=100),
    ]

    resp = client.get(
        "/internal/nodes/read?project_id=proj-1&path=docs",
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "folder"
    names = [c["name"] for c in body["children"]]
    assert ".internal" in names
    assert "guide.md" in names


def test_write_node_content_markdown_requires_string(client, ops, monkeypatch):
    import src.internal.router as _r

    ops.write_file.return_value = SimpleNamespace(commit_id="abc1234567890def")
    monkeypatch.setattr(
        _r,
        "_create_write_commands",
        lambda project_id, user_id: FakeWriteCommands(ops.for_user(project_id, user_id)),
    )

    resp = client.put(
        "/internal/nodes/write",
        json={"project_id": "proj-1", "path": "readme.md", "content": {"bad": "type"}},
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    ops.write_file.assert_awaited_once()


def test_write_node_content_markdown_success(client, ops, monkeypatch):
    import src.internal.router as _r

    monkeypatch.setattr(
        _r,
        "_create_write_commands",
        lambda project_id, user_id: FakeWriteCommands(ops.for_user(project_id, user_id)),
    )
    ops.write_file.return_value = SimpleNamespace(commit_id="abc1234567890def")

    resp = client.put(
        "/internal/nodes/write",
        json={"project_id": "proj-1", "path": "readme.md", "content": "# title"},
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    assert resp.json()["updated"] is True


def test_create_node_folder(client, ops, monkeypatch):
    import src.internal.router as _r

    monkeypatch.setattr(
        _r,
        "_create_write_commands",
        lambda project_id, user_id: FakeWriteCommands(ops.for_user(project_id, user_id)),
    )
    ops.mkdir.return_value = SimpleNamespace(commit_id="abc1234567890def")

    resp = client.post(
        "/internal/nodes/create",
        json={
            "project_id": "proj-1",
            "path": "docs",
            "node_type": "folder",
        },
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    assert resp.json()["created"] is True
    ops.mkdir.assert_awaited_once()


def test_create_node_rejects_unsupported_type(client, ops):
    resp = client.post(
        "/internal/nodes/create",
        json={
            "project_id": "proj-1",
            "path": "bad.bin",
            "node_type": "file",
        },
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 400
    assert "Unsupported node type" in resp.json()["detail"]


def test_remove_node_success(client, ops, monkeypatch):
    import src.internal.router as _r

    monkeypatch.setattr(
        _r,
        "_create_write_commands",
        lambda project_id, user_id: FakeWriteCommands(ops.for_user(project_id, user_id)),
    )
    ops._stat_map["readme.md"] = FakeVersionEntry(
        name="readme.md",
        path="readme.md",
        type="markdown",
    )
    ops.delete.return_value = SimpleNamespace(commit_id="c1")

    resp = client.post(
        "/internal/nodes/rm",
        json={"project_id": "proj-1", "path": "readme.md"},
        headers={"X-Internal-Secret": "ignored"},
    )

    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    ops.delete.assert_awaited_once_with(
        "proj-1",
        ["readme.md"],
        who="mcp_agent",
        message="delete readme.md",
    )
