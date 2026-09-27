from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import settings
from src.provider._base import BaseProvider, Capability, ProviderSpec, FetchResult
from src.provider.materializers.base import MaterializationSchema, MaterializedOutput
from src.provider.materializers.providers import (
    GmailMaterializer,
    GoogleSheetsMaterializer,
)
from src.provider.registry import ProviderRegistry
from src.provider.schemas import Sync
from src.platform.synchronize.engine import SynchronizeEngine


def _sync(provider: str = "gmail") -> Sync:
    return Sync(
        id="sync-1",
        project_id="project-1",
        path="Gmail",
        provider=provider,
        config={},
    )


def test_gmail_materializer_writes_threads_and_index():
    result = FetchResult(
        content={
            "account": "user@example.com",
            "synced_at": "2026-06-09T00:00:00+00:00",
            "email_count": 1,
            "query": "in:inbox",
            "emails": [
                {
                    "id": "msg-1",
                    "thread_id": "thread-1",
                    "subject": "Roadmap",
                    "from": "A <a@example.com>",
                    "to": "B <b@example.com>",
                    "date": "2026-06-09T01:02:03+00:00",
                    "labels": ["INBOX"],
                    "body": "Hello",
                    "url": "https://mail.google.com/mail/u/0/#inbox/msg-1",
                }
            ],
        },
        content_hash="hash-1",
        summary="Fetched 1 email",
    )

    output = GmailMaterializer().materialize(result, _sync())

    assert "_meta/source.json" in output.files
    assert "index.json" in output.files
    assert "inbox/2026/06/thread_thread-1.md" in output.files
    assert output.files["index.json"]["threads"][0]["path"] == "inbox/2026/06/thread_thread-1.md"
    assert "Roadmap" in output.files["inbox/2026/06/thread_thread-1.md"]


def test_google_sheets_materializer_writes_workbook_csv_and_schema():
    result = FetchResult(
        content={
            "synced_at": "2026-06-09T00:00:00+00:00",
            "spreadsheet_id": "sheet-1",
            "spreadsheet_title": "Revenue Plan",
            "sheets": [
                {
                    "name": "Q1",
                    "sheet_id": 123,
                    "headers": ["Month", "Revenue"],
                    "row_count": 1,
                    "rows": [{"Month": "Jan", "Revenue": "100"}],
                }
            ],
        },
        content_hash="hash-2",
    )

    output = GoogleSheetsMaterializer().materialize(result, _sync("google_sheets"))

    assert "spreadsheets/Revenue Plan/workbook.json" in output.files
    assert "spreadsheets/Revenue Plan/sheets/Q1.csv" in output.files
    assert "spreadsheets/Revenue Plan/sheets/Q1.schema.json" in output.files
    assert "Month,Revenue" in output.files["spreadsheets/Revenue Plan/sheets/Q1.csv"]
    assert (
        output.files["index.json"]["sheets"][0]["csv_path"]
        == "spreadsheets/Revenue Plan/sheets/Q1.csv"
    )


class _FakeConnector(BaseProvider):
    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="gmail",
            display_name="Gmail",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
        )

    async def fetch(self, config, credentials):
        return FetchResult(content={}, content_hash="hash")


def test_registry_serializes_materialization_schema():
    registry = ProviderRegistry()
    registry.register(_FakeConnector())
    registry.register_materializer(GmailMaterializer())

    specs = registry.specs_to_dicts()

    assert specs[0]["materialization_schema"]["id"] == "puppyone.gmail.thread_markdown"
    assert specs[0]["materialization_schema"]["latest"] is True
    assert specs[0]["materialization_schemas"][0]["version"] == 1
    assert "index.json" in specs[0]["materialization_schema"]["preview_paths"]


def test_registry_pins_and_resolves_materialization_schema():
    registry = ProviderRegistry()
    registry.register(_FakeConnector())
    registry.register_materializer(GmailMaterializer())

    pinned = registry.pin_materialization_schema("gmail", {"query": "in:inbox"})

    assert pinned["query"] == "in:inbox"
    assert pinned["materialization_schema"] == {
        "id": "puppyone.gmail.thread_markdown",
        "version": 1,
    }
    assert (
        registry.resolve_materializer("gmail", pinned["materialization_schema"]).schema.version == 1
    )


class _EngineConnector(BaseProvider):
    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="engine_fake",
            display_name="Engine Fake",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
        )

    async def fetch(self, config, credentials):
        return FetchResult(content={"ok": True}, content_hash="hash-3", summary="Fetched")


class _CountingConnector(BaseProvider):
    def __init__(self):
        self.fetch_calls = 0

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="counting_fake",
            display_name="Counting Fake",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
        )

    async def fetch(self, config, credentials):
        self.fetch_calls += 1
        return FetchResult(content={"ok": True}, content_hash="hash-counting")


class _ClaimAwareConnector(BaseProvider):
    def __init__(self, run_repo):
        self.run_repo = run_repo
        self.fetch_calls = 0

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="engine_fake",
            display_name="Engine Fake",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
        )

    async def fetch(self, config, credentials):
        assert self.run_repo.claimed == ["run-created"]
        self.fetch_calls += 1
        return FetchResult(content={"ok": True}, content_hash="hash-claim", summary="Fetched")


class _SingleLaneRunRepo:
    def __init__(self, run):
        self.run = run
        self.created: list[tuple[str, str]] = []

    def create_queued_single_lane(self, connection_id: str, trigger_type: str):
        self.created.append((connection_id, trigger_type))
        return self.run, False


class _CreatedSingleLaneRunRepo:
    def __init__(self):
        self.run = SimpleNamespace(id="run-created", status="queued")
        self.created: list[tuple[str, str]] = []
        self.claimed: list[str] = []
        self.completed: list[tuple[str, str, str | None]] = []

    def create_queued_single_lane(self, connection_id: str, trigger_type: str):
        self.created.append((connection_id, trigger_type))
        return self.run, True

    def claim_running(self, run_id: str):
        if run_id != self.run.id or self.run.status != "queued":
            return None
        self.run.status = "running"
        self.claimed.append(run_id)
        return self.run

    def complete(self, run_id: str, *, status: str, result_summary: str | None = None, **_kwargs):
        self.completed.append((run_id, status, result_summary))
        self.run.status = status

    def get_by_id(self, run_id: str):
        return self.run if run_id == self.run.id else None


class _RecordingRuntimeMeter:
    def __init__(self, events: list[str]):
        self.events = events
        self.contexts: list[dict] = []

    async def execute(self, *, audit_context: dict, operation):
        self.contexts.append(audit_context)
        self.events.append("reserve")
        try:
            return await operation()
        finally:
            self.events.append("settle")


class _MeteredConnector(BaseProvider):
    def __init__(self, events: list[str]):
        self.events = events

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="metered_fake",
            display_name="Metered Fake",
            capabilities=Capability.PULL | Capability.PUSH,
            supported_directions=["bidirectional"],
        )

    async def fetch(self, config, credentials):
        self.events.append("fetch")
        return FetchResult(content={"ok": True}, content_hash="metered-hash")

    async def push(self, connection, content, node_type):
        from src.provider.schemas import PushResult

        self.events.append("push")
        return PushResult(success=True, remote_hash="remote-after-push")


class _EngineMaterializer:
    provider = "engine_fake"
    schema = MaterializationSchema(
        id="test.schema",
        version=1,
        label="Test",
        description="Test schema",
        preview_paths=("index.json",),
    )

    def materialize(self, result: FetchResult, sync: Sync) -> MaterializedOutput:
        return MaterializedOutput(
            files={
                "index.json": {"ok": True},
                "docs/item.md": "hello",
            },
            summary="Materialized",
            primary_path="index.json",
            content_hash=result.content_hash,
        )


class _EngineMaterializerV2:
    provider = "engine_fake"
    schema = MaterializationSchema(
        id="test.schema",
        version=2,
        label="Test v2",
        description="Test schema v2",
        preview_paths=("v2/index.json",),
    )

    def materialize(self, result: FetchResult, sync: Sync) -> MaterializedOutput:
        return MaterializedOutput(
            files={"v2/index.json": {"ok": "v2"}},
            summary="Materialized v2",
            primary_path="v2/index.json",
            content_hash=result.content_hash,
        )


@pytest.mark.asyncio
async def test_integration_engine_uses_pinned_materializer():
    registry = ProviderRegistry()
    registry.register(_EngineConnector())
    registry.register_materializer(_EngineMaterializer())
    registry.register_materializer(_EngineMaterializerV2())

    connection = _sync("engine_fake")
    connection.path = "Integrations/Mount"
    connection.config = {
        "target_path": "Integrations/Mount",
        "external_resource_id": "direct:fake",
        "materialization_schema": {"id": "test.schema", "version": 1},
    }

    repository = MagicMock()
    repository.get_by_id.return_value = connection

    run = SimpleNamespace(id="run-1")
    run_repo = MagicMock()
    run_repo.create.return_value = run

    write_port = MagicMock()
    write_port.write_plan = AsyncMock(
        return_value=SimpleNamespace(commit_id="commit-2")
    )

    result = await SynchronizeEngine(
        registry, repository, run_repo, write_port=write_port
    ).execute(connection.id)

    write_port.write_plan.assert_awaited_once()
    call = write_port.write_plan.await_args
    assert set(call.kwargs["plan"].files) == {
        "Integrations/Mount/index.json",
        "Integrations/Mount/docs/item.md",
    }
    assert result["path"] == "Integrations/Mount/index.json"
    repository.update_sync_point.assert_called_once_with(
        sync_id="sync-1",
        last_sync_commit_id="commit-2",
        remote_hash="hash-3",
    )


@pytest.mark.asyncio
async def test_integration_engine_direct_execute_skips_existing_active_run():
    connector = _CountingConnector()
    registry = ProviderRegistry()
    registry.register(connector)

    connection = _sync("counting_fake")
    connection.status = "active"

    repository = MagicMock()
    repository.get_by_id.return_value = connection
    run_repo = _SingleLaneRunRepo(
        SimpleNamespace(id="run-active", status="running", worker_job_id="arq-active")
    )

    result = await SynchronizeEngine(registry, repository, run_repo).execute(connection.id)

    assert result is None
    assert run_repo.created == [("sync-1", "manual")]
    assert connector.fetch_calls == 0
    repository.update_status.assert_not_called()
    repository.update_sync_point.assert_not_called()


@pytest.mark.asyncio
async def test_integration_engine_direct_execute_creates_claims_and_completes_run():
    run_repo = _CreatedSingleLaneRunRepo()
    connector = _ClaimAwareConnector(run_repo)
    registry = ProviderRegistry()
    registry.register(connector)
    registry.register_materializer(_EngineMaterializer())

    connection = _sync("engine_fake")
    connection.path = "Integrations/Mount"
    connection.config = {
        "target_path": "Integrations/Mount",
        "materialization_schema": {"id": "test.schema", "version": 1},
    }

    repository = MagicMock()
    repository.get_by_id.return_value = connection

    write_port = MagicMock()
    write_port.write_plan = AsyncMock(
        return_value=SimpleNamespace(commit_id="commit-claim")
    )

    result = await SynchronizeEngine(
        registry, repository, run_repo, write_port=write_port
    ).execute(connection.id)

    assert result is not None
    assert result["run_id"] == "run-created"
    assert run_repo.created == [("sync-1", "manual")]
    assert run_repo.claimed == ["run-created"]
    assert run_repo.completed == [("run-created", "success", "Fetched")]
    assert connector.fetch_calls == 1
    write_port.write_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_integration_engine_reserves_connector_runtime_before_pull(monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    events: list[str] = []
    meter = _RecordingRuntimeMeter(events)
    connector = _MeteredConnector(events)
    registry = ProviderRegistry()
    registry.register(connector)

    connection = _sync("metered_fake")
    connection.created_by = "user-1"
    repository = MagicMock()
    repository.get_by_id.return_value = connection
    run_repo = _CreatedSingleLaneRunRepo()
    write_port = MagicMock()
    write_port.write_plan = AsyncMock(return_value=SimpleNamespace(commit_id="commit-metered"))

    result = await SynchronizeEngine(
        registry,
        repository,
        run_repo,
        write_port,
        runtime_metering=meter,
    ).execute(connection.id)

    assert result is not None
    assert events == ["reserve", "fetch", "settle"]
    assert meter.contexts == [
        {
            "run_id": "connector:pull:run-created",
            "source": "connector",
            "project_id": "project-1",
            "user_id": "user-1",
            "maximum_runtime_units": 16,
        }
    ]


@pytest.mark.asyncio
async def test_integration_engine_reserves_connector_runtime_before_push(monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    events: list[str] = []
    meter = _RecordingRuntimeMeter(events)
    connector = _MeteredConnector(events)
    registry = ProviderRegistry()
    registry.register(connector)

    connection = _sync("metered_fake")
    connection.direction = "bidirectional"
    connection.created_by = "user-1"
    repository = MagicMock()
    repository.find_owner_by_path.return_value = connection
    run_repo = MagicMock()
    run_repo.create.return_value = SimpleNamespace(id="push-run")

    result = await SynchronizeEngine(
        registry,
        repository,
        run_repo,
        runtime_metering=meter,
    ).push_execute(
        path="/Reports/current.json",
        commit_id="commit-42",
        content={"ok": True},
        node_type="json",
    )

    assert result is not None
    assert events == ["reserve", "push", "settle"]
    assert meter.contexts == [
        {
            "run_id": "connector:push:sync-1:commit-42",
            "source": "connector",
            "project_id": "project-1",
            "user_id": "user-1",
        }
    ]
