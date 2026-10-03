"""Dashboard usage buckets must aggregate every entry-point run log.

Connect rows record runs in ``sync_runs``; scheduled agents record in
``agent_execution_logs``. The dashboard unions both, keyed by entry-point id.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from src.platform.project.dashboard_router import _fetch_connections, _fetch_usage_buckets


class FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._eq_filters = []
        self._in_filters = []
        self._gte_filters = []
        self._order_col = None

    def select(self, _cols):
        return self

    def eq(self, col, value):
        self._eq_filters.append((col, value))
        return self

    def in_(self, col, ids):
        self._in_filters.append((col, set(ids)))
        return self

    def is_(self, col, value):
        self._eq_filters.append((col, None if value == "null" else value))
        return self

    def gte(self, _col, val):
        self._gte_filters.append((_col, val))
        return self

    def order(self, col, **_kwargs):
        self._order_col = col
        return self

    def execute(self):
        out = list(self._rows)
        for col, value in self._eq_filters:
            out = [r for r in out if r.get(col) == value]
        for col, values in self._in_filters:
            out = [r for r in out if r.get(col) in values]
        for col, value in self._gte_filters:
            out = [r for r in out if str(r.get(col, "")) >= value]
        if self._order_col:
            out.sort(key=lambda r: r.get(self._order_col) or "")
        return SimpleNamespace(data=out)


class FakeSB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeTable(self.tables.get(name, []))


def _today_iso() -> str:
    return datetime.now(UTC).isoformat()


def test_usage_buckets_union_connector_and_agent_runs():
    today = _today_iso()
    sb = FakeSB({
        "synchronize_runs": [
            {"synchronize_binding_id": "conn1", "started_at": today},
            {"synchronize_binding_id": "conn1", "started_at": today},
        ],
        "agent_execution_logs": [
            {"agent_id": "agent1", "started_at": today},
            {"agent_id": "agent1", "started_at": today},
            {"agent_id": "agent1", "started_at": today},
        ],
    })

    buckets = _fetch_usage_buckets(sb, ["conn1", "agent1", "mcp1"])

    # last bucket = today
    assert buckets["conn1"][-1] == 2
    assert buckets["agent1"][-1] == 3
    # mcp has no run-log source → stays zero
    assert sum(buckets["mcp1"]) == 0


def test_fetch_connections_never_rehydrates_scope_plaintext_key():
    today = _today_iso()
    sb = FakeSB({
        "synchronize_bindings": [{
            "id": "sync1",
            "project_id": "project-1",
            "provider": "gmail",
            "name": "Gmail",
            "direction": "inbound",
            "status": "active",
            "trigger_type": "scheduled",
            "trigger_config": {"schedule": "0 9 * * *"},
            "config": {},
            "scope_id": "scope-root",
            "last_synced_at": today,
            "created_at": today,
        }],
        "access_surfaces": [{
            "id": "cli1",
            "project_id": "project-1",
            "kind": "cli",
            "name": "FS CLI",
            "status": "active",
            "config": {},
            "scope_id": "scope-root",
            "created_at": today,
        }],
        "repository_scopes": [{
            "id": "scope-root",
            "path": "",
            "max_mode": "r",
        }],
        "synchronize_runs": [{"synchronize_binding_id": "sync1", "started_at": today}],
    })

    rows = _fetch_connections(sb, "project-1")

    assert [row.provider for row in rows] == ["gmail", "cli"]
    assert rows[0].trigger == {"schedule": "0 9 * * *", "type": "scheduled"}
    assert rows[0].usage_buckets[-1] == 1
    assert rows[1].access_key is None
    assert rows[1].has_credential is False
    assert rows[1].credential_hint is None
    assert rows[1].scope_mode == "r"


def test_usage_buckets_one_failing_source_does_not_zero_other():
    today = _today_iso()

    class BoomTable(FakeTable):
        def execute(self):
            raise RuntimeError("relation does not exist")

    class PartialSB(FakeSB):
        def table(self, name):
            if name == "agent_execution_logs":
                return BoomTable([])
            return super().table(name)

    sb = PartialSB({
        "synchronize_runs": [{"synchronize_binding_id": "conn1", "started_at": today}],
    })

    buckets = _fetch_usage_buckets(sb, ["conn1"])
    # sync_runs still counted despite agent_execution_logs erroring
    assert buckets["conn1"][-1] == 1


def test_usage_buckets_empty_ap_ids():
    assert _fetch_usage_buckets(FakeSB({}), []) == {}
