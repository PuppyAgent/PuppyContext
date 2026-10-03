"""Canonical Dashboard usage is domain-qualified; failed reads are not empty success."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.platform.project.dashboard_router import _fetch_uploads
from src.platform.project.resource_dashboard import _usage, fetch_dashboard_resources


class FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def select(self, _columns):
        return self

    def eq(self, column, value):
        self.filters.append(lambda row: row.get(column) == value)
        return self

    def in_(self, column, values):
        self.filters.append(lambda row: row.get(column) in values)
        return self

    def is_(self, column, value):
        return self.eq(column, None if value == "null" else value)

    def gte(self, column, value):
        self.filters.append(lambda row: str(row.get(column, "")) >= value)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=[row for row in self.rows if all(f(row) for f in self.filters)])


class FakeSB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeTable(self.tables.get(name, []))


def test_equal_ids_in_different_domains_do_not_merge_usage():
    today = datetime.now(UTC).isoformat()
    sb = FakeSB({
        "synchronize_bindings": [{
            "id": "equal", "project_id": "p1", "provider": "gmail",
            "target_path": "", "direction": "inbound", "status": "active", "config": {},
        }],
        "access_surfaces": [
            {"id": "equal", "project_id": "p1", "kind": "agent", "config": {}},
            {"id": "mcp", "project_id": "p1", "kind": "mcp", "config": {}},
        ],
        "synchronize_runs": [
            {"synchronize_binding_id": "equal", "project_id": "p1", "started_at": today},
            {"synchronize_binding_id": "equal", "project_id": "p1", "started_at": today},
            {"synchronize_binding_id": "equal", "project_id": "other", "started_at": today},
        ],
        "agent_execution_logs": [{"agent_id": "equal", "started_at": today}] * 3,
    })
    resources = fetch_dashboard_resources(sb, "p1")
    by_identity = {(row.resource_kind, row.resource_id): row for row in resources}
    assert by_identity[("synchronize", "equal")].usage_buckets[-1] == 2
    assert by_identity[("access", "equal")].usage_buckets[-1] == 3
    assert sum(by_identity[("access", "mcp")].usage_buckets) == 0


def test_dashboard_scope_metadata_never_rehydrates_plaintext_key():
    sb = FakeSB({
        "access_surfaces": [{
            "id": "cli", "project_id": "p1", "kind": "cli", "status": "active",
            "scope_id": "scope-docs", "config": {},
        }],
        "repository_scopes": [{
            "id": "scope-docs", "project_id": "p1", "path": "docs", "max_mode": "r",
            "access_key": "historical-secret-must-not-leak",
        }],
    })
    row, = fetch_dashboard_resources(sb, "p1")
    assert row.scope_mode == "r"
    assert row.path == "docs"
    assert row.target.scope_id == "scope-docs"
    assert "access_key" not in row.model_dump()
    assert "historical-secret" not in row.model_dump_json()


def test_usage_read_failure_does_not_become_partial_success():
    class Broken:
        def table(self, _name):
            raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        _usage(Broken(), "synchronize_runs", "synchronize_binding_id", ["binding"], project_id="p1")
    with pytest.raises(RuntimeError, match="unavailable"):
        _fetch_uploads(Broken(), "p1")


def test_empty_domain_id_set_does_not_query_another_log_store():
    class NoQuery:
        def table(self, _name):
            raise AssertionError("empty inventory must not query unrelated history")

    assert _usage(NoQuery(), "synchronize_runs", "synchronize_binding_id", [], project_id="p1") == {}
