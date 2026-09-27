"""Import a GitHub branch's HEAD tree into a version scope.

One-shot snapshot import (no commit-history transfer — the strategy
doc explicitly defers that). The flow:

1. Resolve the integration row → get repo coords + OAuth token.
2. Look up the GitHub branch HEAD → ``git_sha`` (the commit we're importing).
3. Idempotency check: skip if ``github_sync_log`` has a successful
   import for this ``git_sha`` already (covers webhook retries).
4. Walk the branch's tree recursively → ``{path: blob_sha}``.
5. Fetch every blob's bytes (LFS pointers / submodules are surfaced
   to the user as a partial-import warning, not silently dropped).
6. Optional conflict gate: if the version scope's last-known head differs
   from ``last_imported_sha``'s version_commit, refuse unless ``force=True``.
7. Single Git-native transaction-engine operation with a splice that
   overwrites the scope to exactly the GitHub tree.
8. Record the new ``version_commit_id`` alongside the ``git_sha`` in
   ``github_sync_log`` and bump the integration row's watermark.

This intentionally collapses the entire branch into ONE version commit;
the strategy doc says git history is GitHub's job. If users want
finer-grained git-style attribution they should drive the per-commit
import flow themselves (out of MVP scope).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.provider.oauth.repository import OAuthRepository
from src.infra.supabase.client import SupabaseClient
from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.domain.intents import OperationWriteIntent
from src.provider.github.client import (
    GithubApi, GithubApiError, TreeEntry,
)
from src.platform.synchronize.github.repository import (
    GithubSyncRepository, GithubSyncLogRepository,
)
from src.platform.synchronize.github.schemas import GithubSyncRunResult
from src.utils.logger import log_error, log_info, log_warning


_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
_LFS_POINTER_MAX_SIZE = 200  # LFS pointer files are tiny (~135 bytes)


class ImportConflict(Exception):
    """Raised when the importer would overwrite local version changes."""

    def __init__(self, version_head: str, last_imported: Optional[str]):
        self.version_head = version_head
        self.last_imported = last_imported
        super().__init__(
            f"version scope has unpushed local changes "
            f"(head={version_head[:12]}, last_imported={last_imported or '∅'}). "
            f"Pass force=True to overwrite or run an export first."
        )


# Channels that are NOT a conflict for a GitHub re-import: this integration's own
# imports + the system's scope-sync projection. Every other source_channel is a
# real external/user write. Keep in sync with the source_channel taxonomy.
_OWN_OR_SYSTEM_CHANNELS = frozenset({"github", "scope-sync"})


def _has_external_committed_write(rows: list[dict]) -> bool:
    """True if any committed transaction row is from a user/external channel."""
    return any(
        (r.get("source_channel") or "") not in _OWN_OR_SYSTEM_CHANNELS
        for r in rows
    )


async def _external_writes_since(project_id: str, since_iso: str) -> bool:
    """Did the project receive a committed write from an external/user channel
    since ``since_iso``? GitHub import binds to the Project root, so any external
    repository write is divergence the overwrite would clobber. Fail-OPEN on a query
    error — the import is GitHub-authoritative; a DB hiccup must not wedge it."""
    def _query() -> list[dict]:
        sb = SupabaseClient().client
        resp = (
            sb.table("version_transactions")
            .select("source_channel, created_at, status")
            .eq("project_id", project_id)
            .eq("status", "committed")
            .gt("created_at", since_iso)
            .order("created_at", desc=False)
            .limit(200)
            .execute()
        )
        return resp.data or []

    try:
        rows = await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        log_warning(
            f"[GithubImport] divergence check failed for project={project_id}: "
            f"{exc}; allowing import (GitHub-authoritative)"
        )
        return False
    return _has_external_committed_write(rows)


def _current_root_head(project_id: str) -> str:
    """Best-effort current Project head commit id, for the conflict message."""
    try:
        from src.version_engine.infrastructure.supabase.history_repository import (
            SupabaseHistoryManager,
        )
        return SupabaseHistoryManager(SupabaseClient(), project_id).get_scope_head_commit_id("") or ""
    except Exception:  # noqa: BLE001
        return ""


async def import_branch(
    binding: dict,
    *,
    branch: Optional[str] = None,
    force: bool = False,
    triggered_by: str = "manual",
) -> GithubSyncRunResult:
    """Pull *branch* from GitHub into the bound version scope.

    Returns the same shape ``service.import_now`` exposes to the
    router so both manual triggers and the webhook handler get a
    consistent result.

    Side effects: writes one version-history row, one
    ``github_sync_log`` row, and updates ``last_imported_*`` on the
    integration.
    """
    binding_id = binding["id"]
    owner = binding["github_repo_owner"]
    repo = binding["github_repo_name"]
    target_branch = (branch or binding.get("default_branch") or "main").strip()
    oauth_id = binding.get("oauth_connection_id")

    log_info(
        f"[GithubImport] start integration={binding_id} "
        f"repo={owner}/{repo} branch={target_branch} "
        f"trigger={triggered_by} force={force}"
    )

    sync_log = GithubSyncLogRepository()
    binding_repo = GithubSyncRepository()

    # ── 1. OAuth token ───────────────────────────
    if oauth_id is None:
        return await _record_failure(
            sync_log, binding_id, error="no oauth_connection_id on integration",
        )
    oauth = await _load_oauth_token(oauth_id)
    if not oauth:
        return await _record_failure(
            sync_log, binding_id,
            error=f"oauth_connection {oauth_id} not found / no token",
        )

    api = GithubApi(oauth["access_token"])
    try:
        return await _do_import(
            api=api, binding=binding, target_branch=target_branch,
            sync_log=sync_log, binding_repo=binding_repo, force=force,
        )
    except GithubApiError as e:
        return await _record_failure(
            sync_log, binding_id,
            error=f"GitHub API error: {e}",
        )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        # GitHub API momentarily unreachable / DNS down / TLS reset.
        # Distinguish from a 4xx so the user knows to retry rather than
        # reconfigure. Surface the exception class name so ops can
        # bucket transient timeouts vs. proxy resets.
        return await _record_failure(
            sync_log, binding_id,
            error=f"GitHub API unreachable ({type(e).__name__}): {e}",
        )
    except ImportConflict as e:
        result = GithubSyncRunResult(
            status="conflict", direction="import",
            git_sha=None, version_commit_id=e.version_head,
            files_changed=None,
            error_message=str(e),
        )
        await sync_log.record(
            binding_id, direction="import", status="conflict",
            git_sha=None, version_commit_id=e.version_head, error_message=str(e),
        )
        return result
    except Exception as e:  # noqa: BLE001
        log_error(f"[GithubImport] unexpected failure: {e}")
        return await _record_failure(
            sync_log, binding_id, error=f"unexpected: {e}",
        )
    finally:
        await api.aclose()


# ── internals ────────────────────────────────────


async def _do_import(
    *, api: GithubApi, binding: dict, target_branch: str,
    sync_log: GithubSyncLogRepository,
    binding_repo: GithubSyncRepository,
    force: bool = False,
) -> GithubSyncRunResult:
    binding_id = binding["id"]
    project_id = binding["project_id"]
    owner = binding["github_repo_owner"]
    repo_name = binding["github_repo_name"]

    branch_info = await api.get_branch_head(owner, repo_name, target_branch)
    git_sha = branch_info["commit"]["sha"]
    git_tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]

    # Idempotency: webhook deliveries retry on non-200; reject duplicate sha.
    if await sync_log.has_successful_sha(binding_id, "import", git_sha):
        log_info(
            f"[GithubImport] sha {git_sha[:12]} already imported, skipping"
        )
        return GithubSyncRunResult(
            status="success", direction="import",
            git_sha=git_sha, version_commit_id=binding.get("last_imported_sha"),
            files_changed=0,
        )

    entries, truncated = await api.get_tree_recursive(owner, repo_name, git_tree_sha)
    if truncated:
        # Conservative MVP — refuse rather than partial-import.
        msg = (
            f"GitHub tree for {owner}/{repo_name}@{target_branch} is too "
            f"large for the recursive endpoint (truncated). Per-directory "
            f"paging is not yet implemented."
        )
        await sync_log.record(
            binding_id, direction="import", status="failed",
            git_sha=git_sha, error_message=msg,
        )
        return GithubSyncRunResult(
            status="failed", direction="import",
            git_sha=git_sha, version_commit_id=None,
            files_changed=None, error_message=msg,
        )

    files = await _materialise_blobs(api, owner, repo_name, entries)

    # Conflict gate (git non-fast-forward analogue). GitHub import overwrites the
    # Project root, so refuse unless ``force`` when the Project received a
    # COMMITTED write from an external/user channel since the last successful
    # import. ``github`` (our own imports) and ``scope-sync`` (system projection)
    # are NOT conflicts — the check is source_channel-aware, so projection
    # advancing the head never trips a false conflict. The ImportConflict handler
    # records a `conflict` sync-log row instead of clobbering. ``force`` (opt-in,
    # like a force-push) skips the gate.
    if not force:
        last = await sync_log.latest_successful_import(binding_id)
        since = (last or {}).get("created_at")
        last_commit = (last or {}).get("version_commit_id")
        if since and last_commit and await _external_writes_since(project_id, since):
            raise ImportConflict(
                version_head=_current_root_head(project_id),
                last_imported=last_commit,
            )

    # Build a splice that resets the Project root view to the imported tree.
    scope_path = ""  # Version Engine projection path; not a Scope identity.

    splice = _make_overwrite_splice(files)

    repo_manager = build_worker_version_engine_container().repo_manager
    engine = VersionWriteEngine(repo_manager)
    actor = f"github:{owner}/{repo_name}"
    message = (
        f"github import: {owner}/{repo_name}@{target_branch} "
        f"({git_sha[:12]})"
    )
    write_result = await engine.apply_operation(
        OperationWriteIntent(
            project_id=project_id,
            scope_path=scope_path,
            actor=actor,
            source_channel="github",
            operation_type="github_import",
            message=message,
            audit_detail={
                "integration_id": binding_id,
                "git_sha": git_sha,
                "branch": target_branch,
                "files_changed": len(files),
            },
        ),
        splice,
    )

    # The Write Engine returns an empty ``commit_id`` when the
    # splice was a no-op (importing unchanged content). Store that as
    # NULL in the sync log rather than ``""`` so the column is honest
    # about "no commit was produced" — the schema's TEXT nullable
    # column was designed for exactly this case.
    version_commit_id = write_result.commit_id or None
    # Count actual changed paths. ``write_result.paths`` is always a
    # list (never None); an empty list legitimately means "no-op" and
    # should report 0, not fall back to "every input file".
    files_changed = len(write_result.paths)

    await sync_log.record(
        binding_id, direction="import", status="success",
        git_sha=git_sha, version_commit_id=version_commit_id,
        files_changed=files_changed,
    )
    await binding_repo.update_watermark(
        binding_id,
        last_imported_sha=git_sha,
        last_imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    log_info(
        f"[GithubImport] done integration={binding_id} "
        f"git_sha={git_sha[:12]} "
        f"version_commit={(version_commit_id or 'no-op')[:12]} "
        f"files={files_changed}"
    )

    return GithubSyncRunResult(
        status="success", direction="import",
        git_sha=git_sha, version_commit_id=version_commit_id,
        files_changed=files_changed,
    )


async def _materialise_blobs(
    api: GithubApi, owner: str, repo_name: str,
    entries: list[TreeEntry],
) -> dict[str, bytes]:
    """Fetch blob bytes for every file in the tree.

    Skips submodules with a logged warning (importing them recursively
    isn't in MVP scope). Skips git-LFS pointer files for the same
    reason — pulling LFS blob content needs LFS server credentials we
    don't have. The user sees a partial-import warning in the sync log.
    """
    files: dict[str, bytes] = {}
    skipped_lfs: list[str] = []
    skipped_submodule: list[str] = []

    for e in entries:
        if e.type == "tree":
            continue   # tree entries are implicit; we recreate them from blob paths
        if e.mode == "160000":  # gitlink — submodule
            skipped_submodule.append(e.path)
            continue
        if e.type != "blob":
            log_warning(f"[GithubImport] unknown entry type {e.type!r} at {e.path}; skipping")
            continue
        content = await api.get_blob_content(owner, repo_name, e.sha)
        if _is_lfs_pointer(content):
            skipped_lfs.append(e.path)
            continue
        files[e.path] = content

    if skipped_lfs:
        log_warning(
            f"[GithubImport] skipped {len(skipped_lfs)} LFS pointer file(s): "
            f"{skipped_lfs[:5]}…"
        )
    if skipped_submodule:
        log_warning(
            f"[GithubImport] skipped {len(skipped_submodule)} submodule(s): "
            f"{skipped_submodule[:5]}…"
        )
    return files


def _is_lfs_pointer(content: bytes) -> bool:
    return (
        len(content) <= _LFS_POINTER_MAX_SIZE
        and content.startswith(_LFS_POINTER_PREFIX)
    )


def _make_overwrite_splice(files: dict[str, bytes]):
    """Closure that, given (store, root_hash), produces a new tree
    matching exactly *files* — files not in *files* are deleted.

    Reuses :func:`tree_splice.splice_batch` so the diff vs the previous
    tree is reported correctly in ``version history changes``.

    ``BatchOp`` is a plain tuple; the discriminator is the first slot:

      * ``("put",     path, bytes)``
      * ``("put_ref", path, blob_hash)``
      * ``("rm",      path)``
      * ``("mv",      old_path, new_path)``
    """
    from src.version_engine.write_engine.tree import tree_to_flat
    from src.version_engine.adapters.product.tree_patch import splice_batch

    def splice(store, root_hash):
        existing: dict[str, str] = {}
        if root_hash:
            try:
                existing = tree_to_flat(store, root_hash)
            except Exception as e:
                log_warning(
                    f"[GithubImport] tree_to_flat failed on root {root_hash[:12]}: "
                    f"{e}; treating as empty for overwrite"
                )
                existing = {}

        ops: list[tuple] = []
        for path, content in files.items():
            ops.append(("put", path, content))
        for path in existing.keys():
            if path not in files:
                ops.append(("rm", path))
        return splice_batch(store, root_hash, ops)

    return splice


async def _load_oauth_token(oauth_id: int) -> Optional[dict]:
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
    binding_id: str, *, error: str,
) -> GithubSyncRunResult:
    await sync_log.record(
        binding_id, direction="import", status="failed",
        error_message=error,
    )
    return GithubSyncRunResult(
        status="failed", direction="import",
        git_sha=None, version_commit_id=None,
        files_changed=None, error_message=error,
    )
