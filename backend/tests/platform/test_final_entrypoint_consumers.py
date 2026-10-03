"""Final consumers must preserve destination and domain-qualified identities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.platform.synchronize.github.importer import _do_import
from src.platform.synchronize.public_schemas import SynchronizeBindingCreate
from src.platform.synchronize.repository import SynchronizeRepository
from src.platform.synchronize.router import _target_from_request
from src.platform.synchronize.service import SynchronizeService


@pytest.mark.parametrize(
    ("explicit", "config", "expected"),
    [
        ("", {"target_path": "stale"}, ""),
        ("chosen", {"target_path": "stale"}, "chosen"),
        (None, {"target_path": ""}, ""),
        (None, {"target_path": "stored"}, "stored"),
        (None, {}, "URL"),
    ],
)
def test_explicit_destination_wins_and_empty_is_not_missing(explicit, config, expected):
    service = SynchronizeService(MagicMock())
    assert (
        service._default_target_path(
            provider="url",
            config=config,
            fallback_name="URL",
            target_folder_path=explicit,
        )
        == expected
    )
    request = SynchronizeBindingCreate(
        project_id="project", provider="url", config={}, target_path=explicit
    )
    assert _target_from_request(request) == explicit


def test_corrupt_destination_is_not_coerced_into_a_new_path():
    service = SynchronizeService(MagicMock())
    with pytest.raises(ValueError, match="destination must be a string"):
        service._default_target_path(
            provider="url", config={"target_path": 42}, fallback_name="URL", target_folder_path=None
        )


def test_bootstrap_deduplication_is_project_bounded():
    query = MagicMock()
    for name in ("select", "eq", "limit"):
        getattr(query, name).return_value = query
    query.execute.return_value.data = []
    client = MagicMock()
    client.table.return_value = query
    repository = SynchronizeRepository(SimpleNamespace(client=client))
    assert (
        repository.find_by_config_key(
            "url", "external_resource_id", "shared", project_id="project-2"
        )
        is None
    )
    client.table.assert_called_once_with("synchronize_bindings")
    assert ("project_id", "project-2") in [call.args for call in query.eq.call_args_list]
    assert ("external_resource_id", "shared") in [call.args for call in query.eq.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("version_commit", ["version-engine-commit", None])
async def test_github_pull_replay_does_not_fabricate_version_commit_from_git_sha(version_commit):
    api = SimpleNamespace(
        get_branch_head=AsyncMock(
            return_value={
                "commit": {"sha": "external-git-sha", "commit": {"tree": {"sha": "tree"}}},
            }
        ),
        get_tree_recursive=AsyncMock(),
    )
    logs = SimpleNamespace(
        find_successful_sha=AsyncMock(
            return_value={
                "synchronize_github_binding_id": "github-binding",
                "version_commit_id": version_commit,
            }
        )
    )
    result = await _do_import(
        api=api,
        binding={
            "id": "github-binding",
            "project_id": "project",
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
        },
        target_branch="main",
        sync_log=logs,
        binding_repo=MagicMock(),
        force=False,
    )
    assert result.direction == "inbound"
    assert result.git_sha == "external-git-sha"
    assert result.version_commit_id == version_commit
    assert result.files_changed == 0
    logs.find_successful_sha.assert_awaited_once_with(
        "github-binding", "inbound", "external-git-sha"
    )
    api.get_tree_recursive.assert_not_awaited()


class RuntimeStatusQuery:
    def __init__(self, row):
        self.row = row
        self.predicates = []

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, key, value):
        self.predicates.append(lambda: self.row.get(key) == value)
        return self

    def in_(self, key, values):
        self.predicates.append(lambda: self.row.get(key) in values)
        return self

    def execute(self):
        if not all(predicate() for predicate in self.predicates):
            return SimpleNamespace(data=[])
        self.row.update(self.patch)
        return SimpleNamespace(data=[dict(self.row)])


@pytest.mark.parametrize("status", ["active", "syncing", "error", "paused", "disabled"])
def test_runtime_status_is_conditional_and_success_error_cannot_undo_pause(status):
    row = {"id": "binding", "status": status}
    client = MagicMock()
    client.table.side_effect = lambda table: (
        RuntimeStatusQuery(row) if table == "synchronize_bindings" else pytest.fail(table)
    )
    repository = SynchronizeRepository(SimpleNamespace(client=client))
    assert repository.update_runtime_status("binding", "syncing") == (
        status in {"active", "syncing", "error"}
    )
    if status in {"paused", "disabled"}:
        assert row["status"] == status
    # Pause wins even when the worker already read/claimed an active binding.
    row["status"] = "paused"
    repository.update_sync_point("binding", "accepted-commit", "remote-hash")
    assert row["status"] == "paused"
    assert row["last_synchronize_commit_id"] == "accepted-commit"
    assert row["remote_hash"] == "remote-hash"
    repository.update_error("binding", "provider failure")
    assert row["status"] == "paused" and row["error_message"] == "provider failure"
    with pytest.raises(ValueError, match="runtime status"):
        repository.update_runtime_status("binding", "paused")
