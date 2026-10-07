from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.exceptions import NotFoundException
from src.platform.auth.dependencies import get_current_user
from src.platform.authorization.models import ProjectAction
from src.version_engine.bootstrap.dependencies import get_version_admin_service
from src.version_engine.entrypoints.http.content_history import get_project_head, history_router
from src.version_engine.read.history_facts import read_commit_parent_ids
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1
from tests.authorization_fakes import authorization_for, install_authorization


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_head_authorizes_before_read_and_never_reads_history(allowed):
    auth = authorization_for(*(["p"] if allowed else []))
    auth.authorize = Mock(wraps=auth.authorize)
    admin = SimpleNamespace(get_project_head_commit_id=AsyncMock(return_value="a" * 40))
    admin.for_grant = lambda grant: admin
    manager = SimpleNamespace(get_native_service=Mock(return_value=None))
    call = get_project_head("p", admin, auth, SimpleNamespace(user_id="user-1"), manager)
    if allowed:
        response = await call
        assert response.data == {"project_id": "p", "head_commit_id": "a" * 40}
        admin.get_project_head_commit_id.assert_awaited_once_with("p")
    else:
        with pytest.raises(NotFoundException):
            await call
        admin.get_project_head_commit_id.assert_not_awaited()
    auth.authorize.assert_called_once_with("p", "user-1", ProjectAction.HISTORY_READ)


def test_preview_is_opt_in_bounded_and_keeps_full_content_contract():
    app = FastAPI()
    app.include_router(history_router, prefix="/api/v1/content")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="user-1")
    admin = SimpleNamespace(
        get_commit_content=AsyncMock(return_value=b'{"value":"' + b"x" * 100 + b'"}')
    )
    admin.for_grant = lambda grant: admin
    app.dependency_overrides[get_version_admin_service] = lambda: admin
    install_authorization(app, authorization_for("p"))
    with TestClient(app) as client:
        params = {"path": "a.json", "commit_id": "a" * 40, "preview_bytes": 20}
        preview = client.get("/api/v1/content/p/commit-content", params=params)
        assert preview.status_code == 200
        data = preview.json()["data"]
        assert data["truncated"] is True and data["size_bytes"] > 20
        assert "content" not in data and "content_text" not in data
        params.pop("preview_bytes")
        full = client.get("/api/v1/content/p/commit-content", params=params)
        assert full.json()["data"]["content"] == {"value": "x" * 100}
        assert (
            client.get(
                "/api/v1/content/p/commit-content", params={**params, "preview_bytes": 2_000_000}
            ).status_code
            == 422
        )


def test_parent_reads_use_a_verified_batch_and_deduplicate(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    base = store.put_commit(
        f"tree {EMPTY_TREE_SHA1}\nauthor A <a@a> 1 +0000\ncommitter A <a@a> 1 +0000\n\nbase".encode()
    )
    head = store.put_commit(
        f"tree {EMPTY_TREE_SHA1}\nparent {base}\nauthor A <a@a> 2 +0000\ncommitter A <a@a> 2 +0000\n\nhead".encode()
    )
    store.get_objects_many = Mock(wraps=store.get_objects_many)
    store.get_object = Mock(wraps=store.get_object)
    result = read_commit_parent_ids(SimpleNamespace(store=store), [head, base, head, "invalid"])
    assert result == {head: [base], base: [], "invalid": []}
    store.get_objects_many.assert_called_once_with([head, base])
    store.get_object.assert_not_called()


def test_batch_missing_object_falls_back_without_hiding_valid_parents(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    head = store.put_commit(f"tree {EMPTY_TREE_SHA1}\n\nhead".encode())
    result = read_commit_parent_ids(SimpleNamespace(store=store), [head, "f" * 40])
    assert result == {head: [], "f" * 40: []}


def test_batch_objects_retain_hash_integrity_checks(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    oid = store.put_blob(b"valid")
    loose = store.get_loose(oid)
    store.get_loose_many = lambda _: {"f" * 40: loose}
    with pytest.raises(Exception, match="object corrupt"):
        store.get_objects_many(["f" * 40])


def test_catchup_caps_visible_results_to_the_requested_wire_page():
    from src.version_engine.read.native_history import NativeHistory

    history = NativeHistory.__new__(NativeHistory)
    history.head = "149"
    history.nodes = {str(i): {"parent_ids": [str(i - 1)] if i else []} for i in range(150)}
    history.order = [str(i) for i in reversed(range(150))]
    history.entry = lambda oid: {"commit_id": oid}
    page = history.linear(100, since="0")
    assert [row["commit_id"] for row in page] == [str(i) for i in range(1, 101)]
