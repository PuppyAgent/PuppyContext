"""Phase-one ownership gates. Public legacy contracts live at explicit boundaries."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src"
OLD_MODULES = (
    "src.connectors", "src.platform.integrations", "src.repo.github_integration",
    "src.repo.connector_service", "src.repo.connector_repository",
    "src.repo.connector_router", "src.repo.access_surface_repository",
)


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ""
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_no_runtime_import_uses_retired_business_modules():
    violations = [(str(path.relative_to(SRC)), module)
                  for path in SRC.rglob("*.py") for module in imports(path)
                  if any(module == old or module.startswith(old + ".") for old in OLD_MODULES)]
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
    # Serialized capabilities are consumed by unchanged Web/Desktop clients.
    assert ProjectAction.SYNCHRONIZE_MANAGE.value == "integration.manage"
