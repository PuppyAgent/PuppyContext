from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.version_engine.scoped_fs import resolver


class _Query:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _Supabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def table(self, name: str):
        return _Query(self._tables.get(name, []))


def test_resolver_builds_writable_context_when_scope_and_access_are_rw(monkeypatch):
    endpoint = {
        "id": "endpoint-1",
        "name": "Files",
        "project_id": "proj-1",
        "scope_id": "scope-1",
        "status": "active",
        "created_by": "user-1",
        "kind": "mcp",
    }
    sb = _Supabase(
        {
            "repository_scopes": [
                {
                    "id": "scope-1",
                    "project_id": "proj-1",
                    "path": "docs",
                    "exclude": ["private"],
                    "max_mode": "rw",
                }
            ],
        }
    )
    monkeypatch.setattr(resolver, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(
        resolver,
        "_resolve_surface",
        lambda _key: (endpoint, {"fs_policy": {"accesses": [{"readonly": False}]}}),
    )

    with pytest.raises(HTTPException) as exc:
        resolver.resolve_mcp_scoped_fs_context("mcp_key")
    assert exc.value.status_code == 501
    assert exc.value.detail["code"] == "native_scope_not_available"


def test_resolver_downgrades_to_readonly_without_writable_access(monkeypatch):
    endpoint = {
        "id": "endpoint-1",
        "name": "Files",
        "project_id": "proj-1",
        "scope_id": "scope-1",
        "status": "active",
        "kind": "mcp",
    }
    sb = _Supabase(
        {
            "repository_scopes": [
                {
                    "id": "scope-1",
                    "project_id": "proj-1",
                    "path": "",
                    "exclude": [],
                    "max_mode": "rw",
                }
            ],
            "projects": [{"created_by": "project-owner"}],
        }
    )
    monkeypatch.setattr(resolver, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(
        resolver,
        "_resolve_surface",
        lambda _key: (endpoint, {"fs_policy": {"accesses": [{"readonly": True}]}}),
    )

    with pytest.raises(HTTPException) as exc:
        resolver.resolve_mcp_scoped_fs_context("mcp_key")
    assert exc.value.status_code == 501
    assert exc.value.detail["code"] == "native_scope_not_available"


def test_resolver_applies_mcp_tools_config(monkeypatch):
    endpoint = {
        "id": "endpoint-1",
        "name": "Files",
        "project_id": "proj-1",
        "scope_id": "scope-1",
        "status": "active",
        "created_by": "user-1",
        "kind": "mcp",
    }
    sb = _Supabase(
        {
            "repository_scopes": [
                {
                    "id": "scope-1",
                    "project_id": "proj-1",
                    "path": "docs",
                    "exclude": [],
                    "max_mode": "rw",
                }
            ],
        }
    )
    monkeypatch.setattr(resolver, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(
        resolver,
        "_resolve_surface",
        lambda _key: (
            endpoint,
            {
                "fs_policy": {"accesses": [{"readonly": False}]},
                "tools_policy": {"filesystem": {"allowed": ["fs_ls", "fs_rm"]}},
            },
        ),
    )

    with pytest.raises(HTTPException) as exc:
        resolver.resolve_mcp_scoped_fs_context("mcp_key")
    assert exc.value.status_code == 501
    assert exc.value.detail["code"] == "native_scope_not_available"


def test_resolver_rejects_non_mcp_key():
    with pytest.raises(HTTPException) as exc:
        resolver.resolve_mcp_scoped_fs_context("cli_key")

    assert exc.value.status_code == 401


# ── GAP-4: carved child-scope exclusion for MCP keys ──────────────────


def test_merge_scope_excludes_carves_children_not_self_or_siblings():
    out = resolver._merge_scope_excludes(
        ["docs/secret.txt"],
        "docs",
        [{"path": "docs"}, {"path": "docs/api"}, {"path": "docs/api/v1"}, {"path": "other"}],
    )
    assert "docs/api" in out and "docs/api/v1" in out  # declared children carved
    assert "other" not in out and "docs" not in out  # sibling + self never carved
    assert "docs/secret.txt" in out  # user exclude preserved


def test_merge_scope_excludes_root_carves_nothing():
    # Root scope is the project-wide view — it sees all sub-scopes.
    assert resolver._merge_scope_excludes([], "", [{"path": "docs"}, {"path": "docs/api"}]) == []


def test_resolver_carves_child_scopes_for_parent_mcp_key(monkeypatch):
    # An MCP key bound to a NON-ROOT parent scope must hide declared child
    # scopes (GAP-4) — otherwise it could ls/cat/grep into their subtrees.
    endpoint = {
        "id": "endpoint-1",
        "name": "Files",
        "project_id": "proj-1",
        "scope_id": "scope-1",
        "status": "active",
        "created_by": "user-1",
        "kind": "mcp",
    }
    sb = _Supabase(
        {
            "repository_scopes": [
                {
                    "id": "scope-1",
                    "project_id": "proj-1",
                    "path": "docs",
                    "exclude": ["private"],
                    "max_mode": "rw",
                },
                {
                    "id": "scope-2",
                    "project_id": "proj-1",
                    "path": "docs/api",
                    "exclude": [],
                    "max_mode": "rw",
                },
                {
                    "id": "scope-3",
                    "project_id": "proj-1",
                    "path": "other",
                    "exclude": [],
                    "max_mode": "rw",
                },
            ],
        }
    )
    monkeypatch.setattr(resolver, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(
        resolver,
        "_resolve_surface",
        lambda _key: (endpoint, {"fs_policy": {"accesses": [{"readonly": False}]}}),
    )

    with pytest.raises(HTTPException) as exc:
        resolver.resolve_mcp_scoped_fs_context("mcp_key")
    assert exc.value.status_code == 501
    assert exc.value.detail["code"] == "native_scope_not_available"
