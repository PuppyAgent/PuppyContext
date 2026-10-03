"""Durable GitHub pulls, owned and consumed by Synchronize (never Import)."""
from __future__ import annotations

from typing import Any

from src.platform.synchronize.github.importer import import_branch
from src.platform.synchronize.github.repository import GithubSyncRepository
from src.utils.logger import log_error, log_info


async def execute_synchronize_github_pull(
    ctx: dict,
    synchronize_github_binding_id: str,
    *,
    branch: str | None = None,
    force: bool = False,
    triggered_by: str = "webhook",
) -> dict[str, Any]:
    """Reload the binding and preserve importer watermark/conflict/retry semantics.

    Old serialized tasks must drain on their old workers before deployment.
    Unexpected exceptions propagate to ARQ; domain failures retain their durable
    log/status instead of inventing a successful ImportJob result.
    """
    binding = await GithubSyncRepository().get_by_id(synchronize_github_binding_id)
    if not binding:
        log_error(f"[github-pull-job] binding {synchronize_github_binding_id} not found")
        return {"status": "skipped", "reason": "binding_not_found",
                "synchronize_github_binding_id": synchronize_github_binding_id}

    result = await import_branch(binding, branch=branch, force=force, triggered_by=triggered_by)
    log_info(f"[github-pull-job] binding={synchronize_github_binding_id} status={result.status}")
    return {"status": result.status,
            "synchronize_github_binding_id": synchronize_github_binding_id,
            "git_sha": result.git_sha}
