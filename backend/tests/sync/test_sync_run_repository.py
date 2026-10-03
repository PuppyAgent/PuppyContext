from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.platform.synchronize.run_repository import SyncRunRepository

RUNS = "synchronize_runs"
BINDINGS = "synchronize_bindings"


def run_row(id_, binding="conn-1", status="running", **fields):
    return {"id": id_, "synchronize_binding_id": binding, "status": status,
            "triggered_by": "manual", "created_at": "2026-06-03T05:00:00+00:00", **fields}


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.eq_filters = []
        self.in_filters = []
        self.limit_value = None
        self.order_col = None
        self.order_desc = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, col, value):
        self.eq_filters.append((col, value))
        return self

    def in_(self, col, values):
        self.in_filters.append((col, set(values)))
        return self

    def order(self, col, *, desc=False, **_kwargs):
        self.order_col, self.order_desc = col, desc
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        rows = list(self.rows)
        for col, value in self.eq_filters:
            rows = [row for row in rows if row.get(col) == value]
        for col, values in self.in_filters:
            rows = [row for row in rows if row.get(col) in values]
        if self.order_col:
            rows.sort(key=lambda row: row.get(self.order_col) or "", reverse=self.order_desc)
        if self.limit_value is not None:
            rows = rows[:self.limit_value]
        return SimpleNamespace(data=rows)


class FakeClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        # A wrong storage name is a failed query, never a healthy empty store.
        return FakeQuery(self.tables[name])


class InsertRaceQuery(FakeQuery):
    def __init__(self, client, table_name):
        super().__init__(client.tables[table_name])
        self.client, self.table_name = client, table_name
        self.insert_payload = None

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def execute(self):
        if self.insert_payload is not None:
            assert "synchronize_binding_id" in self.insert_payload
            assert "connection_id" not in self.insert_payload
            self.client.insert_attempts += 1
            raise RuntimeError("duplicate key value violates unique constraint")
        if self.table_name == RUNS:
            self.client.active_selects += 1
            if self.client.active_selects == 1:
                return SimpleNamespace(data=[])
        return super().execute()


class InsertRaceClient:
    def __init__(self):
        self.insert_attempts = self.active_selects = 0
        self.tables = {
            BINDINGS: [{"id": "conn-1", "project_id": "project-1", "direction": "inbound"}],
            RUNS: [run_row("run-existing", status="queued", lease_expires_at="2999-01-01T00:00:00+00:00")],
        }

    def table(self, name):
        return InsertRaceQuery(self, name)


class MutableQuery(FakeQuery):
    def __init__(self, client, table_name):
        super().__init__(client.tables[table_name])
        self.client, self.table_name = client, table_name
        self.patch = None

    def update(self, patch):
        self.patch = patch
        return self

    def execute(self):
        if self.patch is None:
            return super().execute()
        rows = list(self.client.tables[self.table_name])
        for col, value in self.eq_filters:
            rows = [row for row in rows if row.get(col) == value]
        for col, values in self.in_filters:
            rows = [row for row in rows if row.get(col) in values]
        for row in rows:
            row.update(self.patch)
        self.client.updates.append((self.table_name, self.patch, [row["id"] for row in rows]))
        return SimpleNamespace(data=rows)


class MutableClient:
    def __init__(self, tables):
        self.tables, self.updates = tables, []

    def table(self, name):
        return MutableQuery(self, name)


def test_failed_history_is_queried_by_canonical_binding_reference():
    repo = SyncRunRepository(SimpleNamespace(client=FakeClient({RUNS: [
        run_row("run-newer", status="failed", started_at="2026-06-03T02:00:00+00:00"),
        run_row("run-ok", status="completed", started_at="2026-06-03T03:00:00+00:00"),
        run_row("run-older", "conn-2", "failed", started_at="2026-06-03T01:00:00+00:00"),
        run_row("run-other", "conn-3", "failed", started_at="2026-06-03T04:00:00+00:00"),
    ]})))
    rows = repo.list_failed_for_connections(["conn-1", "conn-2"], limit=10)
    assert [row.id for row in rows] == ["run-newer", "run-older"]
    assert [row.synchronize_binding_id for row in rows] == ["conn-1", "conn-2"]


def test_get_active_by_sync_returns_newest_active_run():
    repo = SyncRunRepository(SimpleNamespace(client=FakeClient({RUNS: [
        run_row("run-complete", status="completed", created_at="2026-06-03T03:00:00+00:00"),
        run_row("run-queued", status="queued", created_at="2026-06-03T04:00:00+00:00"),
        run_row("run-running"),
        run_row("run-other", "conn-2", created_at="2026-06-03T06:00:00+00:00"),
    ]})))
    run = repo.get_active_by_sync("conn-1")
    assert run is not None and run.id == "run-running"
    assert run.synchronize_binding_id == "conn-1" and run.status == "running"


def test_create_queued_single_lane_falls_back_after_unique_race():
    client = InsertRaceClient()
    run, created = SyncRunRepository(SimpleNamespace(client=client)).create_queued_single_lane("conn-1", trigger_type="manual")
    assert created is False and run.id == "run-existing"
    assert run.synchronize_binding_id == "conn-1" and run.status == "queued"
    assert client.insert_attempts == 1 and client.active_selects == 2


def test_claim_running_only_transitions_queued_run_once():
    client = MutableClient({RUNS: [run_row("run-1", status="queued")]})
    repo = SyncRunRepository(SimpleNamespace(client=client))
    claimed = repo.claim_running("run-1")
    duplicate_claim = repo.claim_running("run-1")
    assert claimed is not None and claimed.id == "run-1" and claimed.status == "running"
    assert duplicate_claim is None
    assert client.tables[RUNS][0]["status"] == "running"
    assert len(client.updates) == 2
    assert client.updates[0][2] == ["run-1"] and client.updates[1][2] == []
    assert client.tables[RUNS][0]["heartbeat_at"] is not None
    assert client.tables[RUNS][0]["lease_expires_at"] is not None


def test_renew_lease_only_updates_running_run():
    client = MutableClient({RUNS: [run_row("run-running"), run_row("run-queued", status="queued")]})
    repo = SyncRunRepository(SimpleNamespace(client=client))
    assert repo.renew_lease("run-running", lease_seconds=60) is True
    assert repo.renew_lease("run-queued", lease_seconds=60) is False
    running, queued = client.tables[RUNS]
    assert running["heartbeat_at"] is not None and running["lease_expires_at"] is not None
    assert "heartbeat_at" not in queued


def test_get_blocking_active_by_sync_recovers_stale_run():
    client = MutableClient({RUNS: [run_row("run-stale", lease_expires_at="2026-06-03T05:10:00+00:00")]})
    repo = SyncRunRepository(SimpleNamespace(client=client))
    assert repo.get_blocking_active_by_sync("conn-1") is None
    assert client.tables[RUNS][0]["status"] == "failed"
    assert client.tables[RUNS][0]["error_message"] == "Sync run lease expired before completion"


def test_is_stale_uses_lease_expiration_before_fallback_age():
    repo = SyncRunRepository(SimpleNamespace(client=FakeClient({})))
    now = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)
    live_run = SimpleNamespace(status="running", lease_expires_at="2026-06-03T06:01:00+00:00",
        heartbeat_at="2026-06-03T01:00:00+00:00", started_at="2026-06-03T01:00:00+00:00", created_at="2026-06-03T01:00:00+00:00")
    stale_run = SimpleNamespace(status="running", lease_expires_at="2026-06-03T05:59:59+00:00",
        heartbeat_at="2026-06-03T05:59:00+00:00", started_at="2026-06-03T05:59:00+00:00", created_at="2026-06-03T05:59:00+00:00")
    legacy_stale_run = SimpleNamespace(status="running", lease_expires_at=None, heartbeat_at=None,
        started_at="2026-06-03T05:00:00+00:00", created_at="2026-06-03T05:00:00+00:00")
    assert repo.is_stale(live_run, lease_seconds=60, now=now) is False
    assert repo.is_stale(stale_run, lease_seconds=60, now=now) is True
    assert repo.is_stale(legacy_stale_run, lease_seconds=60, now=now) is True


def test_recover_stale_active_runs_marks_only_expired_active_runs():
    client = MutableClient({RUNS: [
        run_row("run-stale", lease_expires_at="2000-01-01T00:00:00+00:00"),
        run_row("run-live", "conn-2", lease_expires_at="2999-01-01T00:00:00+00:00"),
        run_row("run-terminal", "conn-3", "failed", lease_expires_at="2000-01-01T00:00:00+00:00"),
    ]})
    recovered = SyncRunRepository(SimpleNamespace(client=client)).recover_stale_active_runs(lease_seconds=60, limit=10)
    assert [run.id for run in recovered] == ["run-stale"]
    assert [row["status"] for row in client.tables[RUNS]] == ["failed", "running", "failed"]


def test_complete_does_not_overwrite_terminal_run():
    client = MutableClient({RUNS: [run_row("run-1", status="failed", error_message="provider failed")]})
    SyncRunRepository(SimpleNamespace(client=client)).complete("run-1", status="success", result_summary="should not overwrite")
    assert client.tables[RUNS][0]["status"] == "failed"
    assert client.tables[RUNS][0]["error_message"] == "provider failed"
    assert client.updates == []


def test_retained_binding_cannot_create_a_run_or_access_a_legacy_table():
    client = FakeClient({BINDINGS: [{"id": "historical", "legacy_read_only_reason": "reviewed historical binding"}]})
    with pytest.raises(RuntimeError, match="LEGACY_BINDING_READ_ONLY"):
        SyncRunRepository(SimpleNamespace(client=client)).create("historical")
