from __future__ import annotations

from contextlib import nullcontext

import pytest
from fastapi import HTTPException

from src.version_engine.domain.errors import PathNotFoundError, VersionReadError
from src.version_engine.entrypoints.http import content_read
from src.version_engine.read.tree_reader import VersionEntry
from src.platform.auth.models import CurrentUser
from tests.authorization_fakes import authorization_for


class _FakeOps:
    def __init__(self) -> None:
        self._files = {
            "config.json": b'{\n  "ok": true\n}',
        }
        self._entries = [
            VersionEntry(name=".env", path=".env", type="file"),
            VersionEntry(name="visible.md", path="visible.md", type="markdown"),
            VersionEntry(name="config.json", path=".config/config.json", type="json"),
        ]

    def open_read(self, _project_id, _grant):
        return nullcontext(self)  # Explicit legacy read fixture.

    def get_read_revision(self, _project_id):
        return None

    def list_dir(self, _project_id: str, _path: str):
        return list(self._entries)

    def list_tree(self, _project_id: str, _path: str, *, max_depth: int = -1):
        return list(self._entries)

    def get_head_commit_id(self, _project_id: str) -> str:
        return "head-1"

    def read_file(self, _project_id: str, path: str) -> bytes:
        return self._files[path]


def _user() -> CurrentUser:
    return CurrentUser(user_id="user-1", role="authenticated")


class _MissingDirOps(_FakeOps):
    def list_dir(self, _project_id: str, _path: str):
        raise PathNotFoundError("directory not found: missing")

    def list_tree(self, _project_id: str, _path: str, *, max_depth: int = -1):
        raise PathNotFoundError("directory not found: missing")


class _ReadUnavailableOps(_FakeOps):
    def list_dir(self, _project_id: str, _path: str):
        raise VersionReadError("db timeout")

    def list_tree(self, _project_id: str, _path: str, *, max_depth: int = -1):
        raise VersionReadError("db timeout")


def test_content_ls_includes_dotfiles_by_default():
    response = content_read.list_dir(
        "project-1",
        path="",
        ops=_FakeOps(),
        authorization=authorization_for("project-1"),
        current_user=_user(),
    )

    paths = [entry.path for entry in response.data.entries]
    assert ".env" in paths
    assert ".config/config.json" in paths
    assert "visible.md" in paths


def test_content_tree_includes_dotfiles_by_default():
    response = content_read.full_tree(
        "project-1",
        path="",
        max_depth=-1,
        ops=_FakeOps(),
        authorization=authorization_for("project-1"),
        current_user=_user(),
    )

    paths = [entry.path for entry in response.data.entries]
    assert ".env" in paths
    assert ".config/config.json" in paths
    assert "visible.md" in paths


def test_content_cat_json_returns_raw_text_and_parsed_content():
    response = content_read.read_file(
        "project-1",
        path="config.json",
        ops=_FakeOps(),
        authorization=authorization_for("project-1"),
        current_user=_user(),
    )

    assert response.data.content == {"ok": True}
    assert response.data.content_text == '{\n  "ok": true\n}'


def test_content_ls_missing_directory_returns_404_not_empty():
    with pytest.raises(HTTPException) as exc:
        content_read.list_dir(
            "project-1",
            path="missing",
            ops=_MissingDirOps(),
            authorization=authorization_for("project-1"),
            current_user=_user(),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == {
        "code": "DIRECTORY_NOT_FOUND",
        "message": "Directory not found: missing",
        "path": "missing",
    }


def test_content_tree_missing_directory_returns_404_not_empty():
    with pytest.raises(HTTPException) as exc:
        content_read.full_tree(
            "project-1",
            path="missing",
            max_depth=-1,
            ops=_MissingDirOps(),
            authorization=authorization_for("project-1"),
            current_user=_user(),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == {
        "code": "DIRECTORY_NOT_FOUND",
        "message": "Directory not found: missing",
        "path": "missing",
    }


def test_content_ls_read_unavailable_returns_502_not_empty():
    with pytest.raises(HTTPException) as exc:
        content_read.list_dir(
            "project-1",
            path="",
            ops=_ReadUnavailableOps(),
            authorization=authorization_for("project-1"),
            current_user=_user(),
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == {
        "code": "VERSION_READ_UNAVAILABLE",
        "message": "Version tree temporarily unavailable while reading project root.",
        "path": "",
    }


def test_content_ls_marked_irrecoverable_returns_explicit_410(monkeypatch):
    monkeypatch.setattr(content_read, "_is_marked_irrecoverable", lambda _project_id: True)

    class _MissingObjectOps(_FakeOps):
        def list_dir(self, _project_id: str, _path: str):
            from src.version_engine.domain.errors import ObjectNotFoundError

            raise ObjectNotFoundError("missing root tree")

    with pytest.raises(HTTPException) as exc:
        content_read.list_dir(
            "project-1",
            path="",
            ops=_MissingObjectOps(),
            authorization=authorization_for("project-1"),
            current_user=_user(),
        )

    assert exc.value.status_code == 410
    assert exc.value.detail["code"] == "VERSION_STORAGE_IRRECOVERABLE"


def test_content_ls_legacy_root_failure_is_explicitly_irrecoverable(monkeypatch):
    monkeypatch.setattr(content_read, "_is_marked_irrecoverable", lambda _project_id: False)
    monkeypatch.setattr(content_read, "_has_unsupported_legacy_root", lambda _project_id: True)

    class _MissingObjectOps(_FakeOps):
        def list_dir(self, _project_id: str, _path: str):
            from src.version_engine.domain.errors import ObjectNotFoundError

            raise ObjectNotFoundError("retired legacy root")

    with pytest.raises(HTTPException) as exc:
        content_read.list_dir(
            "project-1",
            path="",
            ops=_MissingObjectOps(),
            authorization=authorization_for("project-1"),
            current_user=_user(),
        )

    assert exc.value.status_code == 410
    assert exc.value.detail["code"] == "VERSION_STORAGE_IRRECOVERABLE"
