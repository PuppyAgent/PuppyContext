from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from src.infra.supabase import ProjectRepository
from src.platform.authorization.repository import AuthorizationRepository


class _Query:
    def __init__(self, client: _Client, table: str):
        self._client = client
        self._table = table
        self._filters = []
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        self._filters.append(lambda row, c=column, v=value: row.get(c) == v)
        return self

    def in_(self, column, values):
        accepted = set(values)
        self._filters.append(lambda row, c=column, a=accepted: row.get(c) in a)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = [
            deepcopy(row)
            for row in self._client.tables.get(self._table, [])
            if all(predicate(row) for predicate in self._filters)
        ]
        if self._range is not None:
            start, end = self._range
            rows = rows[start : end + 1]
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=rows)


class _Client:
    def __init__(self, **tables):
        self.tables = {name: deepcopy(rows) for name, rows in tables.items()}

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        from tests.authorization_fakes import authorization_facts_response

        return authorization_facts_response(self.tables, name, params)


def _project(lifecycle_status: str) -> dict[str, object]:
    return {
        "id": "project-1",
        "name": "Project",
        "description": None,
        "org_id": "org-1",
        "visibility": "org",
        "bound_git_branch": "main",
        "created_by": "user-1",
        "created_at": "2026-07-16T00:00:00+00:00",
        "updated_at": "2026-07-16T00:00:00+00:00",
        "share_token": "prj_test",
        "lifecycle_status": lifecycle_status,
    }


def test_initializing_project_is_invisible_until_atomic_ready_transition():
    client = _Client(
        projects=[_project("initializing")],
        org_members=[{"org_id": "org-1", "user_id": "user-1", "role": "owner"}],
        project_members=[
            {
                "org_id": "org-1",
                "project_id": "project-1",
                "user_id": "user-1",
                "role": "admin",
            }
        ],
    )
    projects = ProjectRepository(client)
    authorization = AuthorizationRepository(client)

    assert projects.get_by_id("project-1") is None
    assert projects.get_list(org_id="org-1") == []
    assert authorization.load_project_facts("project-1", "user-1") is None
    assert authorization.load_project_facts_batch(["project-1"], "user-1") == {}

    # This models complete_project_initialization's transactionally coupled
    # lifecycle transition. The exact same ordinary reads now publish it.
    client.tables["projects"][0]["lifecycle_status"] = "ready"

    assert projects.get_by_id("project-1").id == "project-1"
    assert [project.id for project in projects.get_list(org_id="org-1")] == ["project-1"]
    assert authorization.load_project_facts("project-1", "user-1") is not None
    assert list(authorization.load_project_facts_batch(["project-1"], "user-1")) == ["project-1"]


def test_startup_never_reconstructs_a_missing_project_root():
    main_source = (Path(__file__).parents[2] / "src/main.py").read_text(encoding="utf-8")
    assert "_init_version_trees" not in main_source
    assert "version_root_hash" not in main_source
