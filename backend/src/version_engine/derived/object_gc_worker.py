"""Scheduled runner for Git-native object garbage collection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import settings
from src.infra.supabase.client import SupabaseClient
from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
from src.version_engine.derived.object_gc import GitObjectGcResult, run_git_object_gc
from src.utils.logger import log_error, log_info, log_warning


def process_object_gc_projects(
    *,
    repo_manager=None,
    client=None,
    project_ids: list[str] | None = None,
    dry_run: bool | None = None,
    retention_seconds: int | None = None,
    max_projects: int | None = None,
    max_delete_per_project: int | None = None,
) -> list[GitObjectGcResult]:
    """Run one GC pass across a bounded set of projects.

    The job is disabled by default and dry-runs by default. Production can turn
    it on gradually by first observing dry-run metrics, then flipping
    ``VERSION_OBJECT_GC_DRY_RUN=false`` after the root set is validated.
    """

    if not settings.VERSION_OBJECT_GC_ENABLED and project_ids is None:
        return []

    repos = repo_manager or build_worker_version_engine_container().repo_manager
    db = client or SupabaseClient().client
    ids = project_ids or _list_project_ids(
        db,
        limit=max_projects or settings.VERSION_OBJECT_GC_MAX_PROJECTS_PER_RUN,
    )
    allowlist = {
        value.strip()
        for value in settings.VERSION_OBJECT_GC_PROJECT_ALLOWLIST.split(",")
        if value.strip()
    }
    if allowlist:
        ids = [project_id for project_id in ids if project_id in allowlist]
    requested_dry_run = settings.VERSION_OBJECT_GC_DRY_RUN if dry_run is None else dry_run
    retention = (
        settings.VERSION_OBJECT_GC_RETENTION_SECONDS
        if retention_seconds is None
        else retention_seconds
    )
    max_delete = (
        settings.VERSION_OBJECT_GC_MAX_DELETE_PER_PROJECT
        if max_delete_per_project is None
        else max_delete_per_project
    )

    results: list[GitObjectGcResult] = []
    for project_id in ids:
        try:
            # Production evidence is tenant-local. A healthy project's dry-run
            # history must never authorize deletion in another project, and an
            # explicitly supplied project list must not bypass this safety gate.
            project_dry_run = requested_dry_run
            if (
                not project_dry_run
                and settings.APP_ENV == "production"
                and not _destructive_rollout_allowed(
                    db,
                    project_id=project_id,
                    required_days=settings.VERSION_OBJECT_GC_REQUIRED_DRY_RUN_DAYS,
                )
            ):
                log_warning(
                    f"[object-gc] project={project_id} destructive rollout gate "
                    "not satisfied; forcing dry-run"
                )
                project_dry_run = True
            repo = repos.get_gc_repo(project_id)
            result = run_git_object_gc(
                repo,
                dry_run=project_dry_run,
                retention_seconds=retention,
                max_delete=max_delete,
                quarantine_seconds=settings.VERSION_OBJECT_GC_QUARANTINE_SECONDS,
            )
            results.append(result)
            if result.unreachable_count or result.deleted_count or result.errors:
                log_info(
                    f"[object-gc] project={project_id} dry_run={project_dry_run} "
                    f"total={result.total_objects} "
                    f"reachable={result.reachable_count} "
                    f"unreachable={result.unreachable_count} "
                    f"eligible={result.eligible_count} "
                    f"quarantined={result.quarantined_count} "
                    f"deleted={result.deleted_count} "
                    f"young={result.kept_young_count} "
                    f"unknown_age={result.kept_unknown_age_count} "
                    f"protected_descendants={result.kept_protected_descendant_count} "
                    f"skipped_for_safety={result.sweep_skipped_for_safety} "
                    f"errors={len(result.errors)}"
                )
            _record_gc_run(db, result)
        except Exception as exc:  # noqa: BLE001 - one project must not stop the pass.
            log_warning(f"[object-gc] project {project_id} failed: {exc}")

    return results


def _record_gc_run(client, result: GitObjectGcResult) -> None:
    """Persist operational evidence and per-project storage trend inputs."""

    try:
        client.table("version_object_gc_runs").insert({
            "project_id": result.project_id,
            "dry_run": result.dry_run,
            "total_objects": result.total_objects,
            "reachable_objects": result.reachable_count,
            "unreachable_objects": result.unreachable_count,
            "eligible_objects": result.eligible_count,
            "quarantined_objects": result.quarantined_count,
            "deleted_objects": result.deleted_count,
            "unreachable_bytes": result.unreachable_bytes,
            "eligible_bytes": result.eligible_bytes,
            "deleted_bytes": result.deleted_bytes,
            "sweep_skipped_for_safety": result.sweep_skipped_for_safety,
            "errors": result.errors,
        }).execute()
    except Exception as exc:  # noqa: BLE001 - metrics must not stop GC.
        log_warning(f"[object-gc] failed to persist run metrics: {exc}")


def _destructive_rollout_allowed(
    client,
    *,
    project_id: str,
    required_days: int,
) -> bool:
    """Require consecutive clean dry-run evidence for exactly one project."""

    required = max(1, int(required_days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=required)
    try:
        response = (
            client.table("version_object_gc_runs")
            .select("created_at, errors, sweep_skipped_for_safety")
            .eq("project_id", project_id)
            .eq("dry_run", True)
            .gte("created_at", cutoff.isoformat())
            .execute()
        )
        rows = response.data or []
        if any(row.get("errors") or row.get("sweep_skipped_for_safety") for row in rows):
            return False
        covered_days = {
            datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            ).date()
            for row in rows
            if row.get("created_at")
        }
        today = datetime.now(timezone.utc).date()
        required_dates = {today - timedelta(days=offset) for offset in range(required)}
        return required_dates.issubset(covered_days)
    except Exception as exc:  # noqa: BLE001 - production deletion fails closed.
        log_warning(f"[object-gc] rollout evidence unavailable: {exc}")
        return False


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
    except Exception as exc:  # noqa: BLE001
        log_error(f"[object-gc] failed to list projects: {exc}")
        return []
