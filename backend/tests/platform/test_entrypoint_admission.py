"""Exercise admission at application/worker boundaries, not just UI discovery."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.platform.imports.repository import ImportJob
from src.platform.imports.runner import OneTimeImportRunner
from src.platform.imports.schemas import ImportJobCreateRequest
from src.platform.imports.service import ImportJobService
from src.platform.synchronize.service import SynchronizeService
from src.provider._base import BaseProvider, Capability, FetchResult, ProviderSpec
from src.provider.github.adapter import GithubProvider
from src.provider.registry import ProviderRegistry
from tests.authorization_fakes import authorization_for


class Adapter(BaseProvider):
    def __init__(self, provider="url", capabilities=Capability.PULL):
        self.provider = provider
        self.capabilities = capabilities
        self.fetch_called = False

    def spec(self):
        return ProviderSpec(provider=self.provider, display_name=self.provider,
                            capabilities=self.capabilities, supported_directions=["inbound"])

    async def fetch(self, config, credentials):
        self.fetch_called = True
        return FetchResult(content="hello", content_hash="hash")


def catalog(*adapters):
    registry = ProviderRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "scheduled"])
async def test_import_only_github_rejected_before_binding_persistence(mode):
    repo = Mock()
    service = SynchronizeService(repo)
    service.register_provider(GithubProvider(github_service=None, s3_service=None))
    with pytest.raises(ValueError, match="[Ss]ynchronize"):
        await service.create_connection("project-1", "github", {}, sync_mode=mode)
    repo.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"sync_mode": "scheduled", "trigger": {"type": "import_once"}},
    {"sync_mode": "manual", "trigger": {"type": "scheduled"}},
    {"sync_mode": "scheduled", "trigger": {"type": "scheduled", "schedule": "a b c d e"}},
    {"sync_mode": "realtime"},
    {"direction": "outbound"},
])
async def test_mode_direction_and_trigger_cannot_bypass_service_admission(options):
    repo = Mock()
    service = SynchronizeService(repo)
    service.register_provider(Adapter())
    with pytest.raises(ValueError):
        await service.create_connection("project-1", "url", {
            "source": {"resource_url": "https://example.com"}, "options": {},
        }, **options)
    repo.create.assert_not_called()


@pytest.mark.parametrize("mode,trigger", [
    ("import_once", {}), ("realtime", {}),
    ("manual", {"type": "scheduled"}),
    ("scheduled", {"schedule": "a b c d e"}),
])
def test_trigger_update_cannot_bypass_domain_admission(mode, trigger):
    repo = Mock()
    repo.get_by_id.return_value = SimpleNamespace(provider="url", direction="inbound",
        config={"source": {"resource_url": "https://example.com"}, "options": {}})
    service = SynchronizeService(repo)
    service.register_provider(Adapter())
    with pytest.raises(ValueError):
        service.update_trigger("binding-1", mode=mode, trigger=trigger)
    repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_catalog_adapter_not_automatically_admitted_to_synchronize():
    repo = Mock()
    service = SynchronizeService(repo)
    service.register_provider(Adapter("new-source"))
    with pytest.raises(ValueError):
        await service.create_connection("project-1", "new-source", {})
    repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_import_rejects_unknown_provider_before_enqueue():
    repo, queue = Mock(), SimpleNamespace(enqueue_import=AsyncMock())
    service = ImportJobService(repo=repo, authorization=authorization_for("project-1", role="editor"),
                               arq_client=queue)
    with pytest.raises(HTTPException) as error:
        await service.create(ImportJobCreateRequest(project_id="project-1",
            provider="new-source", source_url="https://example.com"), "user-1")
    assert error.value.status_code == 400
    repo.create.assert_not_called()
    queue.enqueue_import.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_worker_revalidates_catalog_capability(monkeypatch):
    adapter = Adapter("url", Capability.PUSH)
    monkeypatch.setattr("src.platform.imports.runner.get_import_provider_registry",
                        lambda: catalog(adapter))
    with pytest.raises(ValueError):
        await OneTimeImportRunner().run(ImportJob(id="job", project_id="project-1",
            created_by="user-1", provider="url", source_url="https://example.com"))
    assert not adapter.fetch_called


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [None, Adapter("github"), Adapter("url", Capability.PUSH)])
async def test_synchronize_worker_records_rejected_capability_without_fetch(adapter):
    from src.platform.synchronize.engine import SynchronizeEngine

    repo, runs, writer = Mock(), Mock(), Mock()
    repo.get_by_id.return_value = SimpleNamespace(
        id="binding", status="active", provider="url" if adapter is None else adapter.provider,
        trigger={"type": "manual"}, direction="inbound",
        config={"source": {"resource_url": "https://example.com"}, "options": {}},
    )
    registry = catalog(*([adapter] if adapter else []))
    result = await SynchronizeEngine(registry, repo, runs, writer).execute("binding", run_id="run")
    assert result is None
    assert repo.update_error.call_count == 1
    assert runs.complete.call_args.kwargs["status"] == "failed"
    writer.write_plan.assert_not_called()
    if adapter:
        assert not adapter.fetch_called


@pytest.mark.asyncio
async def test_shared_adapter_explicitly_admitted_without_copying_or_mutating_catalog(monkeypatch):
    from src.provider import dependencies
    from src.platform.imports.providers import get_import_provider_registry
    from src.platform.synchronize.providers import get_synchronize_provider_registry, synchronize_specs

    shared, unknown = Adapter(), Adapter("future")
    registry = catalog(shared, unknown)
    monkeypatch.setattr(dependencies, "_registry_instance", registry)
    imports = get_import_provider_registry()
    synchronize = get_synchronize_provider_registry()
    assert imports.get("url") is synchronize.get("url") is shared
    assert imports.get("future") is synchronize.get("future") is None
    assert registry.get("future") is unknown
    assert [spec["provider"] for spec in synchronize_specs(registry)] == ["url"]
    repo = Mock()
    service = SynchronizeService(repo)
    service.register_provider(shared)
    await service.create_connection("project-1", "url", {
        "source": {"resource_url": "https://example.com"}, "options": {},
    })
    repo.create.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_cancel_delegates_to_import_service(monkeypatch):
    from src.ingest.schemas import SourceType
    from src.ingest.service import IngestService

    called = Mock()
    monkeypatch.setattr(ImportJobService, "cancel", called)
    service = IngestService(Mock(), authorization_for("project-1", role="editor"))
    service._import_job_repo = Mock()
    assert await service.cancel_task("job", SourceType.URL, "user-1")
    called.assert_called_once_with("job", "user-1")
    service._import_job_repo.mark_cancelled.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", None])
async def test_legacy_cancel_keeps_denied_response_without_state_write(role):
    from src.ingest.schemas import SourceType
    from src.ingest.service import IngestService

    authorization = authorization_for("project-1", role=role) if role else authorization_for()
    service = IngestService(Mock(), authorization)
    service._import_job_repo = Mock()
    service._import_job_repo.get.return_value = ImportJob(
        id="job", project_id="project-1", created_by="user-1", provider="url",
        source_url="https://example.com",
    )
    assert not await service.cancel_task("job", SourceType.URL, "user-1")
    service._import_job_repo.mark_cancelled.assert_not_called()
