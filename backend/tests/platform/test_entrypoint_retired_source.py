"""Retired transports cannot be accidentally remounted through operation modules."""

import importlib
import importlib.util

import pytest
from fastapi import APIRouter


@pytest.mark.parametrize("module", [
    "synchronize.router",
    "synchronize.github.router",
    "imports.database.router",
    "access.router",
    "access.project_router",
    "project.dashboard_router",
])
def test_operations_do_not_export_retired_routers(module):
    operations = importlib.import_module(f"src.platform.{module}")
    assert not any(isinstance(value, APIRouter) for value in vars(operations).values())


@pytest.mark.parametrize("module", ["activity.router", "imports.database.schemas"])
def test_obsolete_transport_and_database_dto_modules_are_removed(module):
    assert importlib.util.find_spec(f"src.platform.{module}") is None


def test_binding_repository_does_not_reexport_legacy_identity_aliases():
    from src.platform.synchronize import repository, schemas
    from src.platform.synchronize.service import SynchronizeService

    for name in ("SourceConnection", "SyncRun", "SyncRunRepository"):
        assert not hasattr(repository, name)
    for name in ("SyncResponse", "SyncRunResponse", "CreateSyncResponse", "ProjectSyncStatusResponse"):
        assert not hasattr(schemas, name)
    service = SynchronizeService(object())
    assert not hasattr(service, "sync_repo")
    assert not hasattr(service, "create_sync")
