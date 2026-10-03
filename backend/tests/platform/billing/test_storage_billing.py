from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import settings
from src.exceptions import AppException, ErrorCode
from src.platform.billing.storage import (
    StorageQuotaService,
    StorageReconciliationService,
    StorageUsage,
)


class _Entitlements:
    def __init__(self, limit: int | None, file_limit: int | None = None) -> None:
        self.limit = limit
        self.file_limit = file_limit

    def limit_value(self, org_id: str, key: str):
        assert org_id == "org-1"
        if key == "storage.max_bytes":
            return self.limit
        if key == "upload.max_single_file_bytes":
            return self.file_limit
        raise AssertionError(f"unexpected entitlement key: {key}")

    def get_snapshot(self, org_id: str):
        assert org_id == "org-1"
        return SimpleNamespace(
            source_revision=7,
            entitlements={
                "limits": {
                    "storage.max_bytes": self.limit,
                    "upload.max_single_file_bytes": self.file_limit,
                }
            },
        )


class _UnavailableEntitlements:
    def get_snapshot(self, org_id: str):
        assert org_id == "org-1"
        raise AppException(
            code=ErrorCode.INTERNAL_SERVER_ERROR,
            status_code=503,
            message="snapshot unavailable",
            details={"code": "entitlement_snapshot_missing"},
        )

    def limit_value(self, org_id: str, key: str):
        raise AppException(
            code=ErrorCode.INTERNAL_SERVER_ERROR,
            status_code=503,
            message="snapshot unavailable",
            details={"code": "entitlement_snapshot_missing"},
        )


class _Usage:
    def __init__(self, value: int, version: int = 1) -> None:
        self.row = StorageUsage(org_id="org-1", value=value, version=version)

    def get(self, org_id: str):
        assert org_id == "org-1"
        return self.row


class _Projects:
    def get_by_id(self, project_id: str):
        return SimpleNamespace(id=project_id, org_id="org-1")

    def get_by_org_id(self, org_id: str):
        assert org_id == "org-1"
        return [
            SimpleNamespace(id="project-1", org_id=org_id),
            SimpleNamespace(id="project-2", org_id=org_id),
        ]


class _RepoManager:
    def __init__(self) -> None:
        self.repos = {
            "project-2": SimpleNamespace(
                store=object(),
                get_root_hash=lambda: "other-root",
            )
        }

    def get_server_repo(self, project_id: str):
        return self.repos[project_id]


@pytest.mark.asyncio
async def test_required_mode_defers_atomic_org_quota_check_but_allows_deletion(
    monkeypatch,
) -> None:
    from src.platform.billing import storage as module

    monkeypatch.setattr(settings, "STORAGE_ENFORCEMENT_MODE", "required")
    monkeypatch.setattr(module, "ProjectRepositorySupabase", _Projects)
    sizes = {"old": 90, "larger": 110, "smaller": 70}
    monkeypatch.setattr(module, "logical_tree_bytes", lambda _store, root: sizes[root])
    monkeypatch.setattr(
        module,
        "logical_tree_delta",
        lambda _store, old, new: sizes[new] - sizes[old],
    )
    service = StorageQuotaService(
        entitlement_service=_Entitlements(100),
        usage_repository=_Usage(90),
    )

    growth = await service.check_publish(
        project_id="project-1",
        repo_manager=_RepoManager(),
        store=object(),
        old_root_hash="old",
        new_root_hash="larger",
    )
    assert growth is not None
    assert growth.shadow_would_deny
    assert growth.entitlement_source_revision == 7

    measurement = await service.check_publish(
        project_id="project-1",
        repo_manager=_RepoManager(),
        store=object(),
        old_root_hash="old",
        new_root_hash="smaller",
    )
    assert measurement is not None
    assert measurement.new_org_bytes == 70
    assert measurement.delta_bytes == -20


@pytest.mark.asyncio
async def test_shadow_mode_records_would_deny_without_blocking(monkeypatch) -> None:
    from src.platform.billing import storage as module

    monkeypatch.setattr(settings, "STORAGE_ENFORCEMENT_MODE", "shadow")
    monkeypatch.setattr(module, "ProjectRepositorySupabase", _Projects)
    sizes = {"old": 90, "larger": 110}
    monkeypatch.setattr(module, "logical_tree_bytes", lambda _store, root: sizes[root])
    monkeypatch.setattr(
        module,
        "logical_tree_delta",
        lambda _store, old, new: sizes[new] - sizes[old],
    )
    measurement = await StorageQuotaService(
        entitlement_service=_Entitlements(100),
        usage_repository=_Usage(90),
    ).check_publish(
        project_id="project-1",
        repo_manager=_RepoManager(),
        store=object(),
        old_root_hash="old",
        new_root_hash="larger",
    )

    assert measurement is not None
    assert measurement.shadow_would_deny
    assert measurement.new_org_bytes == 110


@pytest.mark.asyncio
async def test_shadow_mode_does_not_block_when_snapshot_is_missing(monkeypatch) -> None:
    from src.platform.billing import storage as module

    monkeypatch.setattr(settings, "STORAGE_ENFORCEMENT_MODE", "shadow")
    monkeypatch.setattr(module, "ProjectRepositorySupabase", _Projects)
    sizes = {"old": 90, "larger": 110}
    monkeypatch.setattr(module, "logical_tree_bytes", lambda _store, root: sizes[root])
    monkeypatch.setattr(
        module,
        "logical_tree_delta",
        lambda _store, old, new: sizes[new] - sizes[old],
    )

    measurement = await StorageQuotaService(
        entitlement_service=_UnavailableEntitlements(),
        usage_repository=_Usage(90),
    ).check_publish(
        project_id="project-1",
        repo_manager=_RepoManager(),
        store=object(),
        old_root_hash="old",
        new_root_hash="larger",
    )

    assert measurement is not None
    assert measurement.limit_bytes is None
    assert measurement.file_limit_bytes is None
    assert not measurement.shadow_would_deny


@pytest.mark.asyncio
async def test_required_mode_fails_closed_when_snapshot_is_missing(monkeypatch) -> None:
    from src.platform.billing import storage as module

    monkeypatch.setattr(settings, "STORAGE_ENFORCEMENT_MODE", "required")
    monkeypatch.setattr(module, "ProjectRepositorySupabase", _Projects)
    sizes = {"old": 90, "larger": 110}
    monkeypatch.setattr(module, "logical_tree_bytes", lambda _store, root: sizes[root])
    monkeypatch.setattr(
        module,
        "logical_tree_delta",
        lambda _store, old, new: sizes[new] - sizes[old],
    )

    with pytest.raises(AppException) as caught:
        await StorageQuotaService(
            entitlement_service=_UnavailableEntitlements(),
            usage_repository=_Usage(90),
        ).check_publish(
            project_id="project-1",
            repo_manager=_RepoManager(),
            store=object(),
            old_root_hash="old",
            new_root_hash="larger",
        )

    assert caught.value.details == {"code": "entitlement_snapshot_missing"}


@pytest.mark.asyncio
async def test_required_mode_rejects_new_file_above_plan_limit(monkeypatch) -> None:
    from src.platform.billing import storage as module

    monkeypatch.setattr(settings, "STORAGE_ENFORCEMENT_MODE", "required")
    monkeypatch.setattr(module, "ProjectRepositorySupabase", _Projects)
    sizes = {"old": 40, "new": 100}
    monkeypatch.setattr(module, "logical_tree_bytes", lambda _store, root: sizes[root])
    monkeypatch.setattr(
        module,
        "logical_tree_delta",
        lambda _store, old, new: sizes[new] - sizes[old],
    )
    monkeypatch.setattr(
        module,
        "oversized_new_logical_file",
        lambda *_args: ("imports/large.bin", 60),
    )

    with pytest.raises(AppException) as caught:
        await StorageQuotaService(
            entitlement_service=_Entitlements(1_000, file_limit=50),
            usage_repository=_Usage(40),
        ).check_publish(
            project_id="project-1",
            repo_manager=_RepoManager(),
            store=object(),
            old_root_hash="old",
            new_root_hash="new",
        )

    assert caught.value.details == {
        "code": "file_size_limit_exceeded",
        "org_id": "org-1",
        "path": "imports/large.bin",
        "file_bytes": 60,
        "limit_bytes": 50,
        "retryable": False,
    }


def test_oversized_file_check_allows_rename_but_rejects_logical_copy(monkeypatch) -> None:
    from src.platform.billing import storage as module

    manifests = {
        "old": {"old-name.bin": "large-oid"},
        "rename": {"new-name.bin": "large-oid"},
        "copy": {"old-name.bin": "large-oid", "copy.bin": "large-oid"},
    }
    def blob_paths(_store, root, *, include_gitlinks):
        assert include_gitlinks is False
        return manifests[root]

    monkeypatch.setattr(module, "tree_to_flat", blob_paths)

    class Store:
        def get(self, oid):
            assert oid == "large-oid"
            return b"x" * 60

    assert module.oversized_new_logical_file(Store(), "old", "rename", 50) is None
    assert module.oversized_new_logical_file(Store(), "old", "copy", 50) == (
        "copy.bin",
        60,
    )


def test_logical_tree_delta_does_not_reread_unchanged_content(monkeypatch) -> None:
    from src.platform.billing import storage as module

    manifests = {
        "old": {"same.txt": "same", "removed.bin": "removed"},
        "new": {"same.txt": "same", "added.bin": "added"},
    }
    def blob_paths(_store, root, *, include_gitlinks):
        assert include_gitlinks is False
        return manifests[root]

    monkeypatch.setattr(module, "tree_to_flat", blob_paths)

    class Store:
        def __init__(self) -> None:
            self.reads = []

        def get(self, oid):
            self.reads.append(oid)
            return {"removed": b"x" * 20, "added": b"x" * 45}[oid]

    store = Store()
    assert module.logical_tree_delta(store, "old", "new") == 25
    assert set(store.reads) == {"removed", "added"}


@pytest.mark.asyncio
async def test_missing_counter_is_baselined_across_all_org_projects(monkeypatch) -> None:
    from src.platform.billing import storage as module

    monkeypatch.setattr(settings, "STORAGE_ENFORCEMENT_MODE", "shadow")
    monkeypatch.setattr(module, "ProjectRepositorySupabase", _Projects)
    sizes = {"old": 40, "new": 50, "other-root": 25}
    monkeypatch.setattr(module, "logical_tree_bytes", lambda _store, root: sizes[root])
    monkeypatch.setattr(
        module,
        "logical_tree_delta",
        lambda _store, old, new: sizes[new] - sizes[old],
    )

    measurement = await StorageQuotaService(
        entitlement_service=_Entitlements(100),
        usage_repository=_Usage(0, version=0),
    ).check_publish(
        project_id="project-1",
        repo_manager=_RepoManager(),
        store=object(),
        old_root_hash="old",
        new_root_hash="new",
    )

    assert measurement is not None
    assert measurement.old_org_bytes == 65
    assert measurement.new_org_bytes == 75


@pytest.mark.asyncio
async def test_periodic_reconciliation_claims_and_sets_absolute_org_usage(monkeypatch) -> None:
    from src.platform.billing import storage as module

    class Usage:
        def __init__(self) -> None:
            self.calls = []

        def claim_reconciliation_batch(self, *, limit, min_age_seconds):
            assert limit == 10
            assert min_age_seconds == 3600
            return ["org-1"]

        def reconcile(self, **values):
            self.calls.append(values)
            return {"outcome": "reconciled", "value": values["value"]}

    class Repos:
        def get_server_repo(self, project_id):
            return SimpleNamespace(
                store=object(),
                get_root_hash=lambda: f"root-{project_id}",
            )

    usage = Usage()
    monkeypatch.setattr(
        module,
        "logical_tree_bytes",
        lambda _store, root: {"root-project-1": 40, "root-project-2": 25}[root],
    )
    summary = await StorageReconciliationService(
        repo_manager=Repos(),
        entitlement_service=_Entitlements(100),
        usage_repository=usage,
        project_repository=_Projects(),
    ).reconcile_once(limit=10, min_age_seconds=3600)

    assert summary == {"claimed": 1, "reconciled": 1, "failed": 0}
    assert usage.calls[0]["value"] == 65
    assert usage.calls[0]["limit"] == 100
    assert usage.calls[0]["source"] == "storage_reconciler"
    assert usage.calls[0]["idempotency_key"].startswith("storage-full:")
