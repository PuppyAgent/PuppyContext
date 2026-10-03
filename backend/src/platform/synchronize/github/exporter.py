"""Export the bound Version Engine scope's current tree to a GitHub branch.

One commit per export. The flow:

1. Resolve integration → repo coords + OAuth token + branch name.
2. List the version scope's files via :class:`VersionTreeReader` — the
   server-side authoritative read path (S3-backed, cache-aware).
3. For each file, ``POST /repos/.../git/blobs`` with the raw bytes.
   GitHub returns a blob SHA per file. (Existing-blob short-circuit
   isn't worth the complexity at MVP volumes.)
4. ``POST /repos/.../git/trees`` with all (path, blob_sha) entries
   and the current branch HEAD's tree as ``base_tree`` so unchanged
   paths under the project root are preserved.
5. ``POST /repos/.../git/commits`` with the new tree, the current
   branch HEAD as parent, and the configured author identity.
6. ``PATCH /repos/.../git/refs/heads/<branch>`` to fast-forward the
   branch ref. ``force=False`` so we surface non-FF as a clear error.
7. Persist a ``synchronize_github_logs`` row + bump ``last_pushed_*``.

PR-mode (when the branch is protected and direct push is forbidden)
is a documented gap — we surface the GitHub 422 error and let the
user know to switch the integration to PR mode (which we'll add as
a follow-up).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime

from src.exceptions import AppException
from src.platform.project.write_lease import (
    ProjectWriteLease,
    ProjectWriteLeaseFactory,
)
from src.provider.github.client import GithubApi, GithubApiError
from src.platform.synchronize.github.repository import (
    GithubSyncRepository,
    GithubSyncLogRepository,
)
from src.platform.synchronize.github.schemas import GithubSyncRunResult
from src.utils.logger import log_error, log_info
from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container


async def export_to_branch(
    binding: dict, *,
    branch: str | None = None,
    message: str | None = None,
    triggered_by: str = "manual",
    write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
) -> GithubSyncRunResult:
    binding_id = binding["id"]
    project_id = binding["project_id"]
    owner = binding["github_repo_owner"]
    repo = binding["github_repo_name"]
    target_branch = (branch or binding.get("default_branch") or "main").strip()

    log_info(
        f"[GithubExport] start integration={binding_id} "
        f"repo={owner}/{repo} branch={target_branch} trigger={triggered_by}"
    )

    return await _await_true_completion(
        _export_with_write_lease(
            binding=binding,
            target_branch=target_branch,
            message=message,
            project_id=project_id,
            owner=owner,
            repo_name=repo,
            write_lease_factory=write_lease_factory,
        )
    )


async def _export_with_write_lease(
    *,
    binding: dict,
    target_branch: str,
    message: str | None,
    project_id: str,
    owner: str,
    repo_name: str,
    write_lease_factory: ProjectWriteLeaseFactory,
) -> GithubSyncRunResult:
    # Admission is the first side-effecting step. Even an export that later
    # fails OAuth validation must be rejected once Project deletion closes.
    async with write_lease_factory(project_id, "github.export"):
        return await _export_after_admission(
            binding=binding,
            target_branch=target_branch,
            message=message,
            project_id=project_id,
            owner=owner,
            repo_name=repo_name,
        )


async def _export_after_admission(
    *,
    binding: dict,
    target_branch: str,
    message: str | None,
    project_id: str,
    owner: str,
    repo_name: str,
) -> GithubSyncRunResult:
    binding_id = binding["id"]
    oauth_id = binding.get("oauth_connection_id")
    sync_log = GithubSyncLogRepository()
    binding_repo = GithubSyncRepository()

    if oauth_id is None:
        return await _record_failure(
            sync_log, binding_id, "no oauth_connection_id on integration",
        )

    from src.platform.synchronize.github.importer import _load_oauth_token
    oauth = await _load_oauth_token(oauth_id)
    if not oauth:
        return await _record_failure(
            sync_log, binding_id, f"oauth_connection {oauth_id} not found",
        )

    api = GithubApi(oauth["access_token"])
    try:
        return await _await_true_completion(
            _do_export_with_failure_recording(
                api=api,
                binding=binding,
                target_branch=target_branch,
                commit_message=message,
                sync_log=sync_log,
                binding_repo=binding_repo,
                project_id=project_id,
                owner=owner,
                repo_name=repo_name,
            )
        )
    finally:
        await _await_true_completion(api.aclose())


async def _do_export_with_failure_recording(
    *,
    api: GithubApi,
    binding: dict,
    target_branch: str,
    commit_message: str | None,
    sync_log: GithubSyncLogRepository,
    binding_repo: GithubSyncRepository,
    project_id: str,
    owner: str,
    repo_name: str,
) -> GithubSyncRunResult:
    binding_id = binding["id"]
    try:
        return await _do_export(
            api=api,
            binding=binding,
            target_branch=target_branch,
            commit_message=commit_message,
            sync_log=sync_log,
            binding_repo=binding_repo,
            project_id=project_id,
            owner=owner,
            repo_name=repo_name,
        )
    except AppException:
        raise
    except GithubApiError as exc:
        return await _record_failure(
            sync_log,
            binding_id,
            f"GitHub API error: {exc}",
        )
    except Exception as exc:
        log_error(f"[GithubExport] unexpected: {exc}")
        return await _record_failure(
            sync_log,
            binding_id,
            f"unexpected: {exc}",
        )


async def _await_true_completion[T](awaitable: Awaitable[T]) -> T:
    """Delay cancellation until an external or threaded operation is done."""

    future = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancelled:
        # A second cancellation must not make the lease appear released while
        # the shielded GitHub/to_thread work is still running.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
        future.result()
        raise cancelled


async def _do_export(
    *, api: GithubApi, binding: dict, target_branch: str,
    commit_message: str | None, sync_log: GithubSyncLogRepository,
    binding_repo: GithubSyncRepository,
    project_id: str, owner: str, repo_name: str,
) -> GithubSyncRunResult:
    binding_id = binding["id"]

    # 1. List the version scope's current contents.
    files = await _list_scope_files(project_id)
    if not files:
        msg = "version scope is empty — nothing to export"
        await sync_log.record(
            binding_id, direction="outbound", status="failed",
            error_message=msg,
        )
        return GithubSyncRunResult(
            status="failed", direction="outbound",
            git_sha=None, version_commit_id=None, files_changed=0,
            error_message=msg,
        )

    # 2. Find the parent commit on the target branch.
    branch_info = await api.get_branch_head(owner, repo_name, target_branch)
    parent_sha = branch_info["commit"]["sha"]
    base_tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]

    # 3. Upload every file as a blob.
    tree_entries: list[dict] = []
    for path, content in files.items():
        blob_sha = await api.create_blob(owner, repo_name, content)
        tree_entries.append({
            "path": path, "mode": "100644", "type": "blob", "sha": blob_sha,
        })

    # 4. Build the tree (using base_tree means unchanged sibling paths
    #    in the target branch but outside the version scope are preserved
    #    — important when the project mirrors only a subdirectory
    #    of the GitHub repo, but at the moment we always export at root).
    tree_sha = await api.create_tree(
        owner, repo_name, tree_entries, base_tree=base_tree_sha,
    )

    # 5. Commit.
    head = _local_head_commit_id(project_id) or "head"
    msg = commit_message or f"Sync from Puppyone ({head[:12]})"
    new_git_sha = await api.create_commit(
        owner, repo_name,
        message=msg, tree_sha=tree_sha, parent_shas=[parent_sha],
    )

    # 6. Fast-forward the branch.
    await api.update_ref(
        owner, repo_name, f"heads/{target_branch}", new_git_sha,
    )

    files_changed = len(tree_entries)
    await sync_log.record(
        binding_id, direction="outbound", status="success",
        git_sha=new_git_sha, version_commit_id=head,
        files_changed=files_changed,
    )
    await binding_repo.update_watermark(
        binding_id,
        last_pushed_sha=new_git_sha,
        last_pushed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

    log_info(
        f"[GithubExport] done integration={binding_id} "
        f"git_sha={new_git_sha[:12]} files={files_changed}"
    )
    return GithubSyncRunResult(
        status="success", direction="outbound",
        git_sha=new_git_sha, version_commit_id=head,
        files_changed=files_changed,
    )


async def _list_scope_files(project_id: str) -> dict[str, bytes]:
    """Walk the Project root view and return ``{path: bytes}``.

    Uses ``tree_to_flat`` directly off the project's current root_hash
    so we don't have to instantiate per-file readers.
    """
    import asyncio

    from src.version_engine.write_engine.tree import tree_to_flat

    repo_manager = build_worker_version_engine_container().repo_manager
    repo = repo_manager.get_server_repo(project_id)
    root_hash = await asyncio.to_thread(repo.get_root_hash) or ""
    if not root_hash:
        return {}

    flat = await asyncio.to_thread(tree_to_flat, repo.store, root_hash)
    files: dict[str, bytes] = {}
    for path, blob_hash in flat.items():
        content = await asyncio.to_thread(repo.store.get, blob_hash)
        files[path] = content
    return files


def _local_head_commit_id(project_id: str) -> str:
    repo_manager = build_worker_version_engine_container().repo_manager
    repo = repo_manager.get_server_repo(project_id)
    try:
        return repo.get_head_commit_id() or ""
    except Exception:
        return ""


async def _record_failure(
    sync_log: GithubSyncLogRepository,
    binding_id: str, error: str,
) -> GithubSyncRunResult:
    await sync_log.record(
        binding_id, direction="outbound", status="failed",
        error_message=error,
    )
    return GithubSyncRunResult(
        status="failed", direction="outbound",
        git_sha=None, version_commit_id=None,
        files_changed=None, error_message=error,
    )
