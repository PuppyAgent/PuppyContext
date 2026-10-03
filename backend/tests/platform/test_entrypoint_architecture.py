"""Phase-one ownership gates. Public legacy contracts live at explicit boundaries."""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src"
OLD_MODULES = (
    "src.connectors", "src.platform.integrations", "src.repo.github_integration",
    "src.repo.connector_service", "src.repo.connector_repository",
    "src.repo.connector_router", "src.repo.access_surface_repository",
    "src.ingest.file", "src.ingest.upload_jobs", "src.ingest.policy.upload_policy",
)


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                package = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
                module = resolve_name("." * node.level + module, package)
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_no_runtime_import_uses_retired_business_modules():
    violations = [(str(path.relative_to(SRC)), module)
                  for path in SRC.rglob("*.py") for module in imports(path)
                  if any(module == old or module.startswith(old + ".") for old in OLD_MODULES)]
    assert not violations


def test_entrypoints_never_import_sibling_lifecycle_implementations():
    domains = {"upload", "imports", "synchronize", "access"}
    violations = []
    for domain in domains:
        forbidden = tuple(f"src.platform.{other}" for other in domains - {domain})
        for path in (SRC / "platform" / domain).rglob("*.py"):
            violations.extend((str(path.relative_to(SRC)), module) for module in imports(path)
                if any(module == name or module.startswith(name + ".") for name in forbidden))
    assert not violations


def test_provider_does_not_import_entrypoint_lifecycle_or_version_engine():
    forbidden = tuple("src.platform." + name for name in ("upload", "imports", "synchronize", "access")) + (
        "src.version_engine", "src.repo", "src.ingest",
    )
    violations = [(str(path.relative_to(SRC)), module)
                  for path in (SRC / "provider").rglob("*.py") for module in imports(path)
                  if any(module == old or module.startswith(old + ".") for old in forbidden)]
    assert not violations


def test_shared_dtos_contain_no_entrypoint_lifecycle_or_workspace_state():
    from dataclasses import fields
    from src.provider._base import ProviderSpec
    from src.provider import schemas
    from src.platform.synchronize.models import SynchronizeBinding
    from src.platform.workspace.sync_models import SyncResult

    for retired in ("Sync", "SyncResult", "NodeSyncMeta", "SyncProjectRequest", "SyncProjectResponse"):
        assert not hasattr(schemas, retired)
    assert {item.name for item in fields(schemas.SourceInput)} == {"config", "credentials"}
    assert {item.name for item in fields(schemas.MaterializationInput)} == {"source", "provenance"}
    assert not {"supported_sync_modes", "default_sync_mode", "default_trigger"} & {
        item.name for item in fields(ProviderSpec)
    }
    assert SynchronizeBinding.__module__ == "src.platform.synchronize.models"
    assert SyncResult.__module__ == "src.platform.workspace.sync_models"


def test_moved_upload_contracts_repository_and_policy_are_domain_owned():
    from src.platform.upload import schemas, repository, policy, jobs
    from src.ingest import schemas as legacy

    assert schemas.UploadInitRequest.__module__ == "src.platform.upload.schemas"
    assert repository.UploadJobRepository.__module__ == "src.platform.upload.repository"
    assert policy.evaluate_batch_limits.__module__ == "src.platform.upload.policy"
    assert jobs.finalize_upload_to_version.__module__ == "src.platform.upload.jobs"
    assert not hasattr(legacy, "UploadInitRequest")
    from src.platform.upload.service import ETLService
    from src.platform.upload.tasks.models import ETLTask
    from src.platform.upload.arq_client import UploadArqClient
    assert ETLService.__module__ == "src.platform.upload.service"
    assert ETLTask.__module__ == "src.platform.upload.tasks.models"
    assert UploadArqClient.__module__ == "src.platform.upload.arq_client"


def test_file_processing_is_neutral_and_upload_does_not_depend_on_legacy_ingest():
    forbidden = ("src.ingest",) + tuple(f"src.platform.{name}" for name in (
        "upload", "imports", "synchronize", "access",
    ))
    for path in (SRC / "infra" / "file_processing").rglob("*.py"):
        assert not [module for module in imports(path)
            if any(module == name or module.startswith(name + ".") for name in forbidden)], path
    for path in (SRC / "platform" / "upload").rglob("*.py"):
        assert not [module for module in imports(path) if module.startswith("src.ingest")], path


def test_queue_and_runtime_policy_is_owned_by_upload_not_shared_processing(monkeypatch):
    from src.infra.file_processing.config import ETLConfig
    from src.platform.upload.config import UploadWorkerConfig
    from src.infra.queue_config import QueueConnectionConfig

    assert not any(token in field for field in ETLConfig.model_fields
                   for token in ("queue", "redis", "timeout", "attempts", "worker", "ttl", "backoff"))
    monkeypatch.setenv("ETL_REDIS_URL", "redis://localhost:6380/2")
    monkeypatch.setenv("ETL_TASK_TIMEOUT", "777")
    monkeypatch.setenv("ETL_ARQ_QUEUE_NAME", "upload-test")
    config = UploadWorkerConfig(_env_file=None)
    assert config.redis_url == QueueConnectionConfig(_env_file=None).redis_url
    assert config.etl_task_timeout == 777
    assert config.etl_arq_queue_name == "upload-test"


def test_business_models_use_canonical_names():
    from dataclasses import fields
    from src.platform.access.models import AccessSurface
    from src.platform.synchronize.run_repository import SyncRun

    assert "kind" in {field.name for field in fields(AccessSurface)}
    assert "provider" not in {field.name for field in fields(AccessSurface)}
    assert "connection_id" in {field.name for field in fields(SyncRun)}
    assert "access_point_id" not in {field.name for field in fields(SyncRun)}
    forbidden = {"Connector", "IntegrationEngine", "IntegrationService", "IntegrationRepository",
                 "IntegrationConnection", "ConnectorRegistry", "BaseConnector"}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                assert node.name not in forbidden, (path, node.name)


def test_import_and_synchronize_share_registered_provider_instances(monkeypatch):
    from src.provider import dependencies
    from src.provider._base import ProviderDeps
    from src.provider.registry import ProviderRegistry
    from src.platform.imports.providers import get_import_provider_registry
    from src.platform.synchronize.providers import get_synchronize_provider_registry

    catalog = ProviderRegistry()
    discovered = dependencies._discover_providers(ProviderDeps(s3_service=object()))
    # A missing/incorrect discovery path used to silently create an empty registry.
    provider_names = {item.adapter.spec().provider for item in discovered}
    assert {
        "github", "gmail", "google_calendar", "google_docs", "google_drive",
        "google_search_console", "google_sheets", "url",
    } <= provider_names
    assert len(provider_names) == len(discovered)
    for item in discovered:
        catalog.register(item.adapter)
    monkeypatch.setattr(dependencies, "_registry_instance", catalog)
    imports_registry = get_import_provider_registry()
    synchronize_registry = get_synchronize_provider_registry()
    assert imports_registry.get("github") is catalog.get("github")
    assert synchronize_registry.get("github") is None
    for name in synchronize_registry.providers():
        assert synchronize_registry.get(name) is imports_registry.get(name)
        assert imports_registry.get(name) is catalog.get(name)


@pytest.mark.parametrize("role,allowed", [("viewer", False), ("editor", False), ("admin", True)])
def test_renamed_management_actions_keep_the_original_role_boundary(role, allowed):
    from src.platform.authorization.models import ACTION_CAPABILITY, ROLE_CAPABILITIES, ProjectAction, ProjectRole

    for action in (ProjectAction.SYNCHRONIZE_MANAGE, ProjectAction.ACCESS_MANAGE, ProjectAction.IMPORT_SOURCE_MANAGE):
        assert (ACTION_CAPABILITY[action] in ROLE_CAPABILITIES[ProjectRole(role)]) is allowed
    # ISSUE-058 changes the public capability spelling, never its role grant.
    assert ProjectAction.SYNCHRONIZE_MANAGE.value == "synchronize.manage"
