from __future__ import annotations

import pytest

from src.platform.billing.storage import (
    StorageReconciliationService,
)


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
async def test_reconciliation_uses_checked_native_inventory_and_continues_after_one_failure():
    from unittest.mock import Mock

    checked = Mock()
    checked.prune.return_value = 0
    checked.reconcile.side_effect = [RuntimeError("unavailable"), {"outcome": "reconciled"}]
    usage = Mock()
    usage.claim_reconciliation_batch.return_value = ["bad-org", "good-org"]
    manager = Mock()
    manager.create_usage_reconciler.return_value = checked
    result = await StorageReconciliationService(
        repo_manager=manager, usage_repository=usage
    ).reconcile_once(
        limit=10,
        min_age_seconds=3600,
    )
    assert result == {"claimed": 2, "reconciled": 1, "failed": 1}
    assert [c.args[0] for c in checked.reconcile.call_args_list] == ["bad-org", "good-org"]
    manager.get_server_repo.assert_not_called()
