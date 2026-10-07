"""Import a GitHub branch's HEAD tree into a repository HEAD.

One-shot snapshot import (no commit-history transfer — the strategy
doc explicitly defers that). The flow:

1. Resolve the binding → get repo coordinates + OAuth token.
2. Look up the GitHub branch HEAD → ``git_sha`` (the commit we're importing).
3. Idempotency check: skip if ``synchronize_github_logs`` has a successful
   import for this ``git_sha`` already (covers webhook retries).
4. Walk the branch's tree recursively → ``{path: blob_sha}``.
5. Fetch every blob's bytes; unsupported LFS pointers / submodules reject
   the entire snapshot before publication.
6. Optional conflict gate: if the repository HEAD's last-known head differs
   from ``last_imported_sha``'s version_commit, refuse unless ``force=True``.
7. Publish a single native operation, with revision CAS, that replaces
   the repository's file snapshot with the imported files.
8. Record the new ``version_commit_id`` alongside the ``git_sha`` in
   ``synchronize_github_logs`` and bump the binding row's watermark.

This intentionally collapses the entire branch into ONE version commit;
the strategy doc says git history is GitHub's job. If users want
finer-grained git-style attribution they should drive the per-commit
import flow themselves (out of MVP scope).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from src.platform.synchronize.github.public_schemas import SynchronizeGithubResult
from src.platform.synchronize.github.repository import (
    GithubSyncLogRepository,
    GithubSyncRepository,
)
from src.provider.github.client import (
    GithubApi,
    GithubApiError,
    TreeEntry,
)
from src.provider.oauth.repository import OAuthRepository
from src.utils.logger import log_error, log_info
from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
_LFS_POINTER_MAX_SIZE = 200  # LFS pointer files are tiny (~135 bytes)


class ImportConflict(Exception):
    """Raised when the importer would overwrite local version changes."""

    def __init__(self, version_head: str, last_imported: str | None):
        self.version_head = version_head
        self.last_imported = last_imported
        super().__init__(
            f"repository HEAD has unpushed local changes "
            f"(head={version_head[:12]}, last_imported={last_imported or '∅'}). "
            f"Pass force=True to overwrite or run an export first."
        )


async def import_branch(
    binding: dict,
    *,
    branch: str | None = None,
    force: bool = False,
    triggered_by: str = "manual",
) -> SynchronizeGithubResult:
    """Pull *branch* from GitHub into the bound repository HEAD.

    Manual and queued triggers return the canonical result with the actual
    execution binding identity, never a subsequent Project lookup.

    Side effects: writes one native Git operation, one
    ``synchronize_github_logs`` row, and updates ``last_pulled_*`` on the
    binding.
    """
    binding_id = binding["id"]
    owner = binding["github_repo_owner"]
    repo = binding["github_repo_name"]
    target_branch = (branch or binding.get("default_branch") or "main").strip()
    oauth_id = binding.get("oauth_connection_id")

    log_info(
        f"[GithubImport] start binding={binding_id} "
        f"repo={owner}/{repo} branch={target_branch} "
        f"trigger={triggered_by} force={force}"
    )

    sync_log = GithubSyncLogRepository()
    binding_repo = GithubSyncRepository()

    # ── 1. OAuth token ───────────────────────────
    if oauth_id is None:
        return await _record_failure(
            sync_log,
            binding_id,
            error="no oauth_connection_id on binding",
        )
    oauth = await _load_oauth_token(oauth_id)
    if not oauth:
        return await _record_failure(
            sync_log,
            binding_id,
            error=f"oauth_connection {oauth_id} not found / no token",
        )

    api = GithubApi(oauth["access_token"])
    try:
        return await _do_import(
            api=api,
            binding=binding,
            target_branch=target_branch,
            sync_log=sync_log,
            binding_repo=binding_repo,
            force=force,
            user_id=oauth["user_id"],
        )
    except GithubApiError as e:
        return await _record_failure(
            sync_log,
            binding_id,
            error=f"GitHub API error: {e}",
        )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        # GitHub API momentarily unreachable / DNS down / TLS reset.
        # Distinguish from a 4xx so the user knows to retry rather than
        # reconfigure. Surface the exception class name so ops can
        # bucket transient timeouts vs. proxy resets.
        return await _record_failure(
            sync_log,
            binding_id,
            error=f"GitHub API unreachable ({type(e).__name__}): {e}",
        )
    except ImportConflict as e:
        result = SynchronizeGithubResult(
            synchronize_github_binding_id=binding_id,
            status="conflict",
            direction="inbound",
            git_sha=None,
            version_commit_id=e.version_head,
            files_changed=None,
            error_message=str(e),
        )
        await sync_log.record(
            binding_id,
            direction="inbound",
            status="conflict",
            git_sha=None,
            version_commit_id=e.version_head,
            error_message=str(e),
        )
        return result
    except Exception as e:
        log_error(f"[GithubImport] unexpected failure: {e}")
        return await _record_failure(
            sync_log,
            binding_id,
            error=f"unexpected: {e}",
        )
    finally:
        await api.aclose()


# ── internals ────────────────────────────────────


async def _do_import(
    *,
    api: GithubApi,
    binding: dict,
    target_branch: str,
    sync_log: GithubSyncLogRepository,
    binding_repo: GithubSyncRepository,
    force: bool = False,
    user_id: str,
) -> SynchronizeGithubResult:
    binding_id = binding["id"]
    project_id = binding["project_id"]
    owner = binding["github_repo_owner"]
    repo_name = binding["github_repo_name"]

    branch_info = await api.get_branch_head(owner, repo_name, target_branch)
    git_sha = branch_info["commit"]["sha"]
    git_tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]

    # Idempotency: webhook deliveries retry on non-200; reject duplicate sha.
    prior = await sync_log.find_successful_sha(binding_id, "inbound", git_sha)
    if prior:
        log_info(f"[GithubImport] sha {git_sha[:12]} already imported, skipping")
        return SynchronizeGithubResult(
            synchronize_github_binding_id=binding_id,
            status="success",
            direction="inbound",
            git_sha=git_sha,
            version_commit_id=prior.get("version_commit_id"),
            files_changed=0,
        )

    from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter

    repo_manager = build_worker_version_engine_container().repo_manager
    ops = ProductOperationAdapter(repo_manager).for_user(
        project_id,
        user_id,
        operation_key=f"github-import:{binding_id}:{target_branch}:{git_sha}",
    )
    message = f"github import: {owner}/{repo_name}@{target_branch} ({git_sha[:12]})"
    receipt = await ops.native_operation_status(
        project_id,
        ops._grant,
        ops.producer_request_key("bulk_write", message),
    )
    if receipt and receipt["status"] == "committed":
        write_result = ops.write_result(receipt["product"])
    else:
        entries, truncated = await api.get_tree_recursive(owner, repo_name, git_tree_sha)
        if truncated:
            # Conservative MVP — refuse rather than partial-import.
            msg = (
                f"GitHub tree for {owner}/{repo_name}@{target_branch} is too "
                f"large for the recursive endpoint (truncated). Per-directory "
                f"paging is not yet implemented."
            )
            await sync_log.record(
                binding_id,
                direction="inbound",
                status="failed",
                git_sha=git_sha,
                error_message=msg,
            )
            return SynchronizeGithubResult(
                synchronize_github_binding_id=binding_id,
                status="failed",
                direction="inbound",
                git_sha=git_sha,
                version_commit_id=None,
                files_changed=None,
                error_message=msg,
            )

        files = await _materialise_blobs(api, owner, repo_name, entries)
        with ops.open_read(project_id, ops._grant) as reader:
            base = reader.get_read_revision(project_id)
            existing = [e.path for e in reader.list_tree(project_id) if e.type != "folder"]
        last = await sync_log.latest_successful_import(binding_id)
        last_commit = (last or {}).get("version_commit_id")
        if not force and last_commit and last_commit != base["expected_oid"]:
            raise ImportConflict(version_head=base["expected_oid"] or "", last_imported=last_commit)
        from src.version_engine.domain.intents import ProjectWriteState

        state = ProjectWriteState(
            project_id, "", repository_grant=ops._grant, repository_revision=base
        )
        write_result = await ops.bulk_write(
            project_id,
            files,
            deleted=[p for p in existing if p not in files],
            project_write_state=state,
            message=message,
        )

    # A no-op still records the observed native commit as the divergence
    # watermark. Changed paths come from the durable native operation receipt.
    version_commit_id = write_result.commit_id or None
    files_changed = len(write_result.paths)

    await sync_log.record(
        binding_id,
        direction="inbound",
        status="success",
        git_sha=git_sha,
        version_commit_id=version_commit_id,
        files_changed=files_changed,
    )
    await binding_repo.update_watermark(
        binding_id,
        last_pulled_sha=git_sha,
        last_pulled_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

    log_info(
        f"[GithubImport] done binding={binding_id} "
        f"git_sha={git_sha[:12]} "
        f"version_commit={(version_commit_id or 'no-op')[:12]} "
        f"files={files_changed}"
    )

    return SynchronizeGithubResult(
        synchronize_github_binding_id=binding_id,
        status="success",
        direction="inbound",
        git_sha=git_sha,
        version_commit_id=version_commit_id,
        files_changed=files_changed,
    )


async def _materialise_blobs(
    api: GithubApi,
    owner: str,
    repo_name: str,
    entries: list[TreeEntry],
) -> dict[str, bytes]:
    """Fetch blob bytes for every file in the tree.

    Unsupported content refuses the whole snapshot before publication, so
    synchronizing an incomplete tree cannot delete existing local files.
    """
    if len(entries) > 100000:
        raise ValueError("GitHub snapshot exceeds the native producer entry limit")
    files: dict[str, bytes] = {}
    total = 0
    for entry in entries:
        if entry.type == "tree":
            continue
        if entry.mode == "160000" or entry.type != "blob":
            raise ValueError(
                "GitHub snapshot contains an unsupported entry; no files were published"
            )
        content = await api.get_blob_content(owner, repo_name, entry.sha)
        if _is_lfs_pointer(content):
            raise ValueError("GitHub snapshot requires LFS content; no files were published")
        total += len(content)
        if len(content) > 64 * 1024**2 or total > 256 * 1024**2:
            raise ValueError("GitHub snapshot exceeds the native producer byte limit")
        files[entry.path] = content

    return files


def _is_lfs_pointer(content: bytes) -> bool:
    return len(content) <= _LFS_POINTER_MAX_SIZE and content.startswith(_LFS_POINTER_PREFIX)


async def _load_oauth_token(oauth_id: int) -> dict | None:
    """Pull access_token (refreshing on demand) for the bound OAuth row.

    Uses :meth:`OAuthRepository.get_by_id` which is async-native, returns
    a typed ``OAuthConnection``, and avoids the older raw-table-query
    path that depended on a private ``.client`` attribute the repository
    doesn't expose.
    """
    try:
        repo = OAuthRepository()
        connection = await repo.get_by_id(oauth_id)
        if not connection:
            return None
        # The importer downstream only needs ``access_token`` plus a
        # couple of identity fields for the GithubApi constructor; flatten
        # the model to a dict so call sites don't need to know about
        # OAuthConnection's pydantic shape.
        return {
            "id": connection.id,
            "user_id": connection.user_id,
            "access_token": connection.access_token,
            "refresh_token": connection.refresh_token,
            "expires_at": connection.expires_at,
            "workspace_name": connection.workspace_name,
        }
    except Exception as e:
        log_error(f"[GithubImport] oauth lookup failed: {e}")
        return None


async def _record_failure(
    sync_log: GithubSyncLogRepository,
    binding_id: str,
    *,
    error: str,
) -> SynchronizeGithubResult:
    await sync_log.record(
        binding_id,
        direction="inbound",
        status="failed",
        error_message=error,
    )
    return SynchronizeGithubResult(
        synchronize_github_binding_id=binding_id,
        status="failed",
        direction="inbound",
        git_sha=None,
        version_commit_id=None,
        files_changed=None,
        error_message=error,
    )
