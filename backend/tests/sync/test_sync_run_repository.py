from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.platform.synchronize.run_repository import SyncRunRepository


class FakeQuery:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.eq_filters: list[tuple[str, object]] = []
        self.in_filters: list[tuple[str, set]] = []
        self.limit_value: int | None = None
        self.order_col: str | None = None
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
        self.order_col = col
        self.order_desc = desc
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
            rows.sort(
                key=lambda row: row.get(self.order_col) or "",
                reverse=self.order_desc,
            )
        if self.limit_value is not None:
            rows = rows[: self.limit_value]
        return SimpleNamespace(data=rows)


class FakeClient:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def table(self, name):
        return FakeQuery(self.tables.get(name, []))


class InsertRaceQuery(FakeQuery):
    def __init__(self, client: "InsertRaceClient", table_name: str):
        super().__init__(client.tables.get(table_name, []))
        self.client = client
        self.table_name = table_name
        self.insert_payload: dict | None = None

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def execute(self):
        if self.insert_payload is not None:
            self.client.insert_attempts += 1
            raise RuntimeError("duplicate key value violates unique constraint")
        if self.table_name == "sync_runs":
            self.client.active_selects += 1
            if self.client.active_selects == 1:
                return SimpleNamespace(data=[])
        return super().execute()


class InsertRaceClient:
    def __init__(self):
        self.insert_attempts = 0
        self.active_selects = 0
        self.tables = {
            "connections": [{
                "id": "conn-1",
                "project_id": "project-1",
                "direction": "inbound",
            }],
            "sync_runs": [{
                "id": "run-existing",
                "connection_id": "conn-1",
                "status": "queued",
                "triggered_by": "manual",
                "created_at": "2026-06-03T05:00:00+00:00",
                "lease_expires_at": "2999-01-01T00:00:00+00:00",
            }],
        }

    def table(self, name):
        return InsertRaceQuery(self, name)


class MutableQuery(FakeQuery):
    def __init__(self, client: "MutableClient", table_name: str):
        super().__init__(client.tables.get(table_name, []))
        self.client = client
        self.table_name = table_name
        self.patch: dict | None = None

    def update(self, patch):
        self.patch = patch
        return self

    def execute(self):
        if self.patch is None:
            return super().execute()
        rows = list(self.client.tables.get(self.table_name, []))
        for col, value in self.eq_filters:
            rows = [row for row in rows if row.get(col) == value]
        for col, values in self.in_filters:
            rows = [row for row in rows if row.get(col) in values]
        for row in rows:
            row.update(self.patch)
        self.client.updates.append((self.table_name, self.patch, [row["id"] for row in rows]))
        return SimpleNamespace(data=rows)


class MutableClient:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.updates: list[tuple[str, dict, list[str]]] = []

    def table(self, name):
        return MutableQuery(self, name)


def test_list_failed_for_access_points_reads_sync_runs_by_connection_id():
    repo = SyncRunRepository(SimpleNamespace(client=FakeClient({
        "sync_runs": [
            {
                "id": "run-newer",
                "connection_id": "conn-1",
                "status": "failed",
                "started_at": "2026-06-03T02:00:00+00:00",
            },
            {
                "id": "run-ok",
                "connection_id": "conn-1",
                "status": "completed",
                "started_at": "2026-06-03T03:00:00+00:00",
            },
            {
                "id": "run-older",
                "connection_id": "conn-2",
                "status": "failed",
                "started_at": "2026-06-03T01:00:00+00:00",
            },
            {
                "id": "run-other",
                "connection_id": "conn-3",
                "status": "failed",
                "started_at": "2026-06-03T04:00:00+00:00",
            },
        ],
    })))

    rows = repo.list_failed_for_connections(["conn-1", "conn-2"], limit=10)

    assert [row.id for row in rows] == ["run-newer", "run-older"]
    assert [row.connection_id for row in rows] == ["conn-1", "conn-2"]


def test_get_active_by_sync_returns_newest_active_run():
    repo = SyncRunRepository(SimpleNamespace(client=FakeClient({
        "sync_runs": [
            {
                "id": "run-complete",
                "connection_id": "conn-1",
                "status": "completed",
                "created_at": "2026-06-03T03:00:00+00:00",
            },
            {
                "id": "run-queued",
                "connection_id": "conn-1",
                "status": "queued",
                "created_at": "2026-06-03T04:00:00+00:00",
            },
            {
                "id": "run-running",
                "connection_id": "conn-1",
                "status": "running",
                "created_at": "2026-06-03T05:00:00+00:00",
            },
            {
                "id": "run-other",
                "connection_id": "conn-2",
                "status": "running",
                "created_at": "2026-06-03T06:00:00+00:00",
            },
        ],
    })))

    run = repo.get_active_by_sync("conn-1")

    assert run is not None
    assert run.id == "run-running"
    assert run.connection_id == "conn-1"
    assert run.status == "running"


def test_create_queued_single_lane_falls_back_after_unique_race():
    client = InsertRaceClient()
    repo = SyncRunRepository(SimpleNamespace(client=client))

    run, created = repo.create_queued_single_lane("conn-1", trigger_type="manual")

    assert created is False
    assert run.id == "run-existing"
    assert run.connection_id == "conn-1"
    assert run.status == "queued"
    assert client.insert_attempts == 1
    assert client.active_selects == 2


def test_claim_running_only_transitions_queued_run_once():
    client = MutableClient({
        "sync_runs": [{
            "id": "run-1",
            "connection_id": "conn-1",
            "status": "queued",
            "triggered_by": "manual",
            "created_at": "2026-06-03T05:00:00+00:00",
        }],
    })
    repo = SyncRunRepository(SimpleNamespace(client=client))

    claimed = repo.claim_running("run-1")
    duplicate_claim = repo.claim_running("run-1")

    assert claimed is not None
    assert claimed.id == "run-1"
    assert claimed.status == "running"
    assert duplicate_claim is None
    assert client.tables["sync_runs"][0]["status"] == "running"
    assert len(client.updates) == 2
    assert client.updates[0][2] == ["run-1"]
    assert client.updates[1][2] == []
    assert client.tables["sync_runs"][0]["heartbeat_at"] is not None
    assert client.tables["sync_runs"][0]["lease_expires_at"] is not None


def test_renew_lease_only_updates_running_run():
    client = MutableClient({
        "sync_runs": [
            {
                "id": "run-running",
                "connection_id": "conn-1",
                "status": "running",
                "triggered_by": "manual",
                "created_at": "2026-06-03T05:00:00+00:00",
            },
            {
                "id": "run-queued",
                "connection_id": "conn-1",
                "status": "queued",
                "triggered_by": "manual",
                "created_at": "2026-06-03T05:00:00+00:00",
            },
        ],
    })
    repo = SyncRunRepository(SimpleNamespace(client=client))

    assert repo.renew_lease("run-running", lease_seconds=60) is True
    assert repo.renew_lease("run-queued", lease_seconds=60) is False

    running = client.tables["sync_runs"][0]
    queued = client.tables["sync_runs"][1]
    assert running["heartbeat_at"] is not None
    assert running["lease_expires_at"] is not None
    assert "heartbeat_at" not in queued


def test_get_blocking_active_by_sync_recovers_stale_run():
    client = MutableClient({
        "sync_runs": [{
            "id": "run-stale",
            "connection_id": "conn-1",
            "status": "running",
            "triggered_by": "manual",
            "created_at": "2026-06-03T05:00:00+00:00",
            "lease_expires_at": "2026-06-03T05:10:00+00:00",
        }],
    })
    repo = SyncRunRepository(SimpleNamespace(client=client))

    run = repo.get_blocking_active_by_sync("conn-1")

    assert run is None
    assert client.tables["sync_runs"][0]["status"] == "failed"
    assert client.tables["sync_runs"][0]["error_message"] == (
        "Sync run lease expired before completion"
    )


def test_is_stale_uses_lease_expiration_before_fallback_age():
    repo = SyncRunRepository(SimpleNamespace(client=FakeClient({})))
    now = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)
    live_run = SimpleNamespace(
        status="running",
        lease_expires_at="2026-06-03T06:01:00+00:00",
        heartbeat_at="2026-06-03T01:00:00+00:00",
        started_at="2026-06-03T01:00:00+00:00",
        created_at="2026-06-03T01:00:00+00:00",
    )
    stale_run = SimpleNamespace(
        status="running",
        lease_expires_at="2026-06-03T05:59:59+00:00",
        heartbeat_at="2026-06-03T05:59:00+00:00",
        started_at="2026-06-03T05:59:00+00:00",
        created_at="2026-06-03T05:59:00+00:00",
    )
    legacy_stale_run = SimpleNamespace(
        status="running",
        lease_expires_at=None,
        heartbeat_at=None,
        started_at="2026-06-03T05:00:00+00:00",
        created_at="2026-06-03T05:00:00+00:00",
    )

    assert repo.is_stale(live_run, lease_seconds=60, now=now) is False
    assert repo.is_stale(stale_run, lease_seconds=60, now=now) is True
    assert repo.is_stale(legacy_stale_run, lease_seconds=60, now=now) is True


def test_recover_stale_active_runs_marks_only_expired_active_runs():
    client = MutableClient({
        "sync_runs": [
            {
                "id": "run-stale",
                "connection_id": "conn-1",
                "status": "running",
                "triggered_by": "manual",
                "created_at": "2026-06-03T05:00:00+00:00",
                "lease_expires_at": "2000-01-01T00:00:00+00:00",
            },
            {
                "id": "run-live",
                "connection_id": "conn-2",
                "status": "running",
                "triggered_by": "manual",
                "created_at": "2026-06-03T05:00:00+00:00",
                "lease_expires_at": "2999-01-01T00:00:00+00:00",
            },
            {
                "id": "run-terminal",
                "connection_id": "conn-3",
                "status": "failed",
                "triggered_by": "manual",
                "created_at": "2026-06-03T05:00:00+00:00",
                "lease_expires_at": "2000-01-01T00:00:00+00:00",
            },
        ],
    })
    repo = SyncRunRepository(SimpleNamespace(client=client))

    recovered = repo.recover_stale_active_runs(lease_seconds=60, limit=10)

    assert [run.id for run in recovered] == ["run-stale"]
    assert client.tables["sync_runs"][0]["status"] == "failed"
    assert client.tables["sync_runs"][1]["status"] == "running"
    assert client.tables["sync_runs"][2]["status"] == "failed"


def test_complete_does_not_overwrite_terminal_run():
    client = MutableClient({
        "sync_runs": [{
            "id": "run-1",
            "connection_id": "conn-1",
            "status": "failed",
            "triggered_by": "manual",
            "created_at": "2026-06-03T05:00:00+00:00",
            "error_message": "provider failed",
        }],
    })
    repo = SyncRunRepository(SimpleNamespace(client=client))

    repo.complete("run-1", status="success", result_summary="should not overwrite")

    assert client.tables["sync_runs"][0]["status"] == "failed"
    assert client.tables["sync_runs"][0]["error_message"] == "provider failed"
    assert client.updates == []
