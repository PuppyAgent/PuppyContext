from types import SimpleNamespace

from src.platform.project import router as project_router


class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.filters: list[tuple[str, set]] = []

    def select(self, *_args, **_kwargs):
        return self

    def in_(self, col, values):
        self.filters.append((col, set(values)))
        return self

    def execute(self):
        rows = list(self.rows)
        for col, values in self.filters:
            rows = [row for row in rows if row.get(col) in values]
        return SimpleNamespace(data=rows)


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def table(self, name):
        return _FakeQuery(self.tables.get(name, []))


def test_project_access_point_count_reads_target_tables(monkeypatch):
    sb = _FakeSupabase({
        "synchronize_bindings": [
            {"project_id": "p1"},
            {"project_id": "p1"},
            {"project_id": "p2"},
        ],
        "access_surfaces": [
            {"project_id": "p1", "kind": "mcp"},
            {"project_id": "p1", "kind": "git_remote"},
            {"project_id": "p2", "kind": "agent"},
        ],
    })
    monkeypatch.setattr(project_router, "get_supabase_client", lambda: sb)

    counts = project_router._count_user_access_points(["p1", "p2"])

    assert counts == {"p1": 3, "p2": 1}
