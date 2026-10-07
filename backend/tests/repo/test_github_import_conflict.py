"""GitHub import uses native revision CAS and durable retry receipts."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.platform.synchronize.github import importer
from src.provider.github.client import TreeEntry
from src.version_engine.adapters.product.operation_adapter import (
    ProductOperationAdapter,
    WriteResult,
)

BINDING = dict(
    id="binding-1", project_id="project-1", github_repo_owner="owner", github_repo_name="repo"
)


def setup_import(monkeypatch, *, receipt=None, head="current"):
    api = SimpleNamespace(
        get_branch_head=AsyncMock(
            return_value={"commit": {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}}
        ),
        get_tree_recursive=AsyncMock(return_value=([], False)),
    )
    logs = SimpleNamespace(
        find_successful_sha=AsyncMock(return_value=None),
        latest_successful_import=AsyncMock(return_value={"version_commit_id": "previous"}),
        record=AsyncMock(),
    )
    binding_repo = SimpleNamespace(update_watermark=AsyncMock())
    ops = Mock()
    ops._grant = object()
    ops.producer_request_key.return_value = "stable-native-request"
    ops.native_operation_status = AsyncMock(return_value=receipt)
    ops.write_result = ProductOperationAdapter.write_result
    reader = SimpleNamespace(
        get_read_revision=lambda _p: {"expected_oid": head},
        list_tree=lambda _p: [SimpleNamespace(path="removed.txt", type="file")],
    )
    ops.open_read.return_value = nullcontext(reader)
    ops.bulk_write = AsyncMock(return_value=WriteResult(commit_id="new", paths=["removed.txt"]))
    monkeypatch.setattr(
        importer,
        "build_worker_version_engine_container",
        lambda: SimpleNamespace(repo_manager=object()),
    )
    keys = []

    def bind(_self, project, user, *, operation_key):
        assert (project, user) == ("project-1", "initiator")
        keys.append(operation_key)
        return ops

    monkeypatch.setattr(ProductOperationAdapter, "for_user", bind)
    return api, logs, binding_repo, ops, keys


@pytest.mark.asyncio
async def test_diverged_native_head_is_not_overwritten(monkeypatch):
    api, logs, binding_repo, ops, keys = setup_import(monkeypatch)
    with pytest.raises(importer.ImportConflict):
        await importer._do_import(
            api=api,
            binding=BINDING,
            target_branch="main",
            sync_log=logs,
            binding_repo=binding_repo,
            user_id="initiator",
        )
    ops.bulk_write.assert_not_awaited()
    assert keys == ["github-import:binding-1:main:" + "a" * 40]


@pytest.mark.asyncio
async def test_force_import_still_passes_captured_native_base(monkeypatch):
    api, logs, binding_repo, ops, _ = setup_import(monkeypatch)
    result = await importer._do_import(
        api=api,
        binding=BINDING,
        target_branch="main",
        sync_log=logs,
        binding_repo=binding_repo,
        user_id="initiator",
        force=True,
    )
    arguments = ops.bulk_write.await_args.kwargs
    assert arguments["project_write_state"].repository_revision == {"expected_oid": "current"}
    assert arguments["deleted"] == ["removed.txt"]
    assert result.version_commit_id == "new" and result.files_changed == 1


@pytest.mark.asyncio
async def test_lost_log_after_success_replays_without_reading_or_overwriting_new_head(monkeypatch):
    receipt = {
        "status": "committed",
        "product": {"commit_oid": "already-published", "changes": [["add", "ZmlsZS50eHQ="]]},
    }
    api, logs, binding_repo, ops, _ = setup_import(monkeypatch, receipt=receipt)
    result = await importer._do_import(
        api=api,
        binding=BINDING,
        target_branch="main",
        sync_log=logs,
        binding_repo=binding_repo,
        user_id="initiator",
    )
    assert result.version_commit_id == "already-published" and result.files_changed == 1
    api.get_tree_recursive.assert_not_awaited()
    ops.open_read.assert_not_called()
    ops.bulk_write.assert_not_awaited()
    assert logs.record.await_args.kwargs["version_commit_id"] == "already-published"
    binding_repo.update_watermark.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", ["submodule", "lfs"])
async def test_incomplete_snapshot_cannot_publish_or_delete_existing_files(
    monkeypatch, unsupported
):
    api, logs, binding_repo, ops, _ = setup_import(monkeypatch)
    entry = TreeEntry(
        path="external",
        sha="c" * 40,
        type="commit" if unsupported == "submodule" else "blob",
        mode="160000" if unsupported == "submodule" else "100644",
    )
    api.get_tree_recursive.return_value = ([entry], False)
    api.get_blob_content = AsyncMock(return_value=b"version https://git-lfs.github.com/spec/v1\n")
    with pytest.raises(ValueError, match="no files were published"):
        await importer._do_import(
            api=api,
            binding=BINDING,
            target_branch="main",
            sync_log=logs,
            binding_repo=binding_repo,
            user_id="initiator",
            force=True,
        )
    ops.bulk_write.assert_not_awaited()
    ops.open_read.assert_not_called()
    binding_repo.update_watermark.assert_not_awaited()
