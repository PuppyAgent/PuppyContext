"""Scheduled background scan for corrupt primary loose objects.

Runbook §8① (docs/ops/bulk-push-520885e2-runbook.md): instead of
waiting for a user to hit a bulk-push ``invalid git loose object``
failure, periodically sweep active projects' primary object prefixes,
verify each loose object, and surface (or optionally heal) corruption.

Disabled by default, diagnosis-only by default. Ops turns it on by
observing the dry-run "ticket" log lines first, then flips
``VERSION_INTEGRITY_SCAN_HEAL=true`` once comfortable that the
detected objects really are unrecoverable stale bytes (the same
human gate the ``/admin/object-integrity`` endpoint enforces).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.config import settings
from src.infra.supabase.client import SupabaseClient
from src.utils.logger import log_error, log_info, log_warning
from src.version_engine.bootstrap.dependencies import (
    build_worker_version_engine_container,
)


@dataclass
class IntegrityScanResult:
    project_id: str
    checked: int = 0
    corrupt: list[str] = field(default_factory=list)
    healed: int = 0
    truncated: bool = False
    supported: bool = True


def process_object_integrity_projects(
    *,
    repo_manager=None,
    client=None,
    project_ids: list[str] | None = None,
    heal: bool | None = None,
    max_projects: int | None = None,
) -> list[IntegrityScanResult]:
    """Run one integrity-scan pass across a bounded set of projects.

    Mirrors ``object_gc_worker.process_object_gc_projects``: disabled +
    diagnosis-only by default, one project's failure never aborts the
    pass, results returned for the scheduler job to summarise.
    """
    if not settings.VERSION_INTEGRITY_SCAN_ENABLED and project_ids is None:
        return []

    repos = repo_manager or build_worker_version_engine_container().repo_manager
    db = client or SupabaseClient().client
    ids = project_ids or _list_project_ids(
        db,
        limit=max_projects or settings.VERSION_INTEGRITY_SCAN_MAX_PROJECTS_PER_RUN,
    )
    do_heal = settings.VERSION_INTEGRITY_SCAN_HEAL if heal is None else heal

    results: list[IntegrityScanResult] = []
    for project_id in ids:
        try:
            results.append(_scan_one_project(repos, project_id, heal=do_heal))
        except Exception as exc:
            log_warning(f"[integrity-scan] project {project_id} failed: {exc}")
    return results


def _scan_one_project(repos, project_id: str, *, heal: bool) -> IntegrityScanResult:
    repo = repos.get_gc_repo(project_id)
    # Unwrap decorator backends (e.g. CachedStorageBackend) to reach the
    # concrete backend that implements the scan; otherwise every project is
    # silently reported unsupported because the wrapper lacks the method.
    backend = getattr(repo.store, "_backend", None) or repo.store
    scan = getattr(backend, "async_scan_primary_loose_integrity", None)
    for _ in range(8):  # bounded unwrap; guards against a cyclic _inner
        if scan is not None:
            break
        inner = getattr(backend, "_inner", None)
        if inner is None or inner is backend:
            break
        backend = inner
        scan = getattr(backend, "async_scan_primary_loose_integrity", None)
    if scan is None:
        return IntegrityScanResult(project_id=project_id, supported=False)

    summary = asyncio.run(scan(heal=heal))
    result = IntegrityScanResult(
        project_id=project_id,
        checked=summary.get("checked", 0),
        corrupt=list(summary.get("corrupt", [])),
        healed=summary.get("healed", 0),
        truncated=summary.get("truncated", False),
        supported=summary.get("supported", True),
    )
    if result.corrupt:
        # The "ticket": one structured line per affected project that
        # ops alerting can pattern-match on. Lists a sample of hashes so
        # the on-call can feed them straight to /admin/object-integrity.
        log_warning(
            f"[integrity-scan] CORRUPT project={project_id} "
            f"checked={result.checked} corrupt={len(result.corrupt)} "
            f"healed={result.healed} heal_enabled={heal} "
            f"sample={result.corrupt[:10]}"
        )
    else:
        log_info(
            f"[integrity-scan] clean project={project_id} "
            f"checked={result.checked} truncated={result.truncated}"
        )
    return result


def _list_project_ids(client, *, limit: int) -> list[str]:
    limit = max(1, min(int(limit or 1), 500))
    try:
        resp = (
            client.table("projects")
            .select("id")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [row["id"] for row in (resp.data or []) if row.get("id")]
    except Exception as exc:
        log_error(f"[integrity-scan] failed to list projects: {exc}")
        return []
