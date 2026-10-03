"""ARQ jobs for durable Synchronize sync runs."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import traceback

from src.config import settings
from src.platform.synchronize.run_repository import SyncRunRepository
from src.infra.supabase.client import SupabaseClient
from src.platform.synchronize.dependencies import create_synchronize_engine
from src.platform.synchronize.engine import SynchronizeEngine

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = {"success", "completed", "failed", "cancelled", "skipped"}


async def _heartbeat_run_lease(
    run_repo: SyncRunRepository,
    run_id: str,
    *,
    interval_seconds: int,
    lease_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            renewed = run_repo.renew_lease(
                run_id,
                lease_seconds=lease_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sync run heartbeat failed for %s: %s", run_id, exc)
            continue
        if not renewed:
            logger.warning("Sync run heartbeat stopped; run no longer active: %s", run_id)
            return


async def execute_synchronize_run(ctx: dict, run_id: str) -> dict:
    """Run a queued durable connection sync."""
    run_repo: SyncRunRepository = ctx.get("sync_run_repository") or SyncRunRepository(
        SupabaseClient()
    )
    engine: SynchronizeEngine = ctx.get("synchronize_engine") or create_synchronize_engine()

    run = run_repo.get_by_id(run_id)
    if not run:
        logger.error("Sync run not found: %s", run_id)
        return {"status": "failed", "error": "run_not_found", "run_id": run_id}

    if run.status in TERMINAL_RUN_STATUSES:
        return {"status": "skipped", "run_id": run_id, "run_status": run.status}

    lease_seconds = settings.SYNC_RUN_LEASE_SECONDS
    if run_repo.is_stale(run, lease_seconds=lease_seconds):
        run_repo.mark_stale(run_id)
        return {
            "status": "failed",
            "run_id": run_id,
            "run_status": "failed",
            "reason": "run_lease_expired",
        }

    claimed = run_repo.claim_running(run_id, lease_seconds=lease_seconds)
    if not claimed:
        refreshed = run_repo.get_by_id(run_id)
        return {
            "status": "skipped",
            "run_id": run_id,
            "run_status": refreshed.status if refreshed else "missing",
            "reason": "run_not_claimed",
        }
    run = claimed

    heartbeat_interval = max(1, min(
        settings.SYNC_RUN_HEARTBEAT_INTERVAL_SECONDS,
        max(1, lease_seconds // 3),
    ))
    heartbeat_task = asyncio.create_task(
        _heartbeat_run_lease(
            run_repo,
            run_id,
            interval_seconds=heartbeat_interval,
            lease_seconds=lease_seconds,
        )
    )

    try:
        result = await engine.execute(
            run.synchronize_binding_id,
            trigger_type=run.trigger_type,
            run_id=run.id,
        )
        refreshed = run_repo.get_by_id(run_id)
        if result:
            logger.info("Sync run completed: %s", run_id)
            return {
                "status": "completed",
                "run_id": run_id,
                "synchronize_binding_id": run.synchronize_binding_id,
                "path": result.get("path"),
                "commit_id": result.get("commit_id"),
            }
        if refreshed and refreshed.status in {"queued", "running"}:
            run_repo.complete(
                run_id,
                status="skipped",
                result_summary="Sync did not run",
            )
            refreshed = run_repo.get_by_id(run_id)
        return {
            "status": (refreshed.status if refreshed else "no_change"),
            "run_id": run_id,
            "synchronize_binding_id": run.synchronize_binding_id,
        }
    except asyncio.CancelledError:
        logger.error("Sync run cancelled by worker timeout: %s", run_id)
        run_repo.complete(run_id, status="failed", error="Sync worker was cancelled or timed out")
        raise
    except Exception as exc:
        logger.error("Sync run failed %s: %s\n%s", run_id, exc, traceback.format_exc())
        run_repo.complete(run_id, status="failed", error=str(exc))
        return {"status": "failed", "run_id": run_id, "error": str(exc)}
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
