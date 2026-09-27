"""
Sync execution job for scheduled sync tasks.

APScheduler calls execute_sync_pull when a scheduled sync is due.
It only creates/enqueues a durable sync run; the sync worker owns execution.
"""

import asyncio
from datetime import datetime, timezone

from src.utils.logger import log_info, log_error


async def _execute_sync_pull_async(sync_id: str, trigger_type: str = "scheduled") -> dict:
    """
    Queue fresh data pull for a scheduled Integration binding.
    """
    from src.platform.synchronize.run_repository import SyncRunRepository
    from src.infra.supabase.client import SupabaseClient
    from src.platform.synchronize.arq_client import SyncArqClient
    from src.platform.synchronize.repository import SynchronizeRepository

    started_at = datetime.now(timezone.utc)
    log_info(f"[sync-scheduler] Queueing pull for sync {sync_id}")
    supabase = SupabaseClient()
    connection = SynchronizeRepository(supabase).get_by_id(sync_id)
    if not connection:
        return {
            "status": "failed",
            "access_point_id": sync_id,
            "error": "Connection not found",
        }
    if connection.direction not in {"inbound", "bidirectional"}:
        return {
            "status": "skipped",
            "access_point_id": sync_id,
            "reason": "not_configured_for_inbound_sync",
        }
    run_repo = SyncRunRepository(supabase)
    active_run = run_repo.get_blocking_active_by_sync(sync_id)
    if active_run:
        return {
            "status": active_run.status,
            "access_point_id": sync_id,
            "connection_id": sync_id,
            "run_id": active_run.id,
            "worker_job_id": active_run.worker_job_id,
            "deduped": True,
            "reason": f"sync_already_{active_run.status}",
        }
    if connection.status not in {"active", "error"}:
        return {
            "status": "skipped",
            "access_point_id": sync_id,
            "reason": f"status_{connection.status}",
        }

    try:
        run, created = run_repo.create_queued_single_lane(
            sync_id,
            trigger_type=trigger_type,
        )
        if not created:
            return {
                "status": run.status,
                "access_point_id": sync_id,
                "connection_id": sync_id,
                "run_id": run.id,
                "worker_job_id": run.worker_job_id,
                "deduped": True,
                "reason": f"sync_already_{run.status}",
            }
        worker_job_id = await SyncArqClient().enqueue_sync_run(run.id)
        run_repo.set_worker_job_id(run.id, worker_job_id)
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

        log_info(f"[sync-scheduler] Pull queued for sync {sync_id} in {elapsed_ms}ms")
        return {
            "status": "queued",
            "access_point_id": sync_id,
            "connection_id": sync_id,
            "run_id": run.id,
            "worker_job_id": worker_job_id,
            "elapsed_ms": elapsed_ms,
        }

    except Exception as e:
        log_error(f"[sync-scheduler] Queueing failed for sync {sync_id}: {e}")
        import traceback
        log_error(f"Traceback: {traceback.format_exc()}")

        try:
            from src.platform.synchronize.repository import SynchronizeRepository
            SynchronizeRepository(SupabaseClient()).update_error(sync_id, str(e))
        except Exception:
            pass

        return {"status": "failed", "access_point_id": sync_id, "error": str(e)}


def execute_sync_pull(sync_id: str, trigger_type: str = "scheduled"):
    """
    Synchronous wrapper for APScheduler (runs in ThreadPoolExecutor).
    """
    log_info(f"[sync-scheduler] Scheduler triggered for sync {sync_id}")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_execute_sync_pull_async(sync_id, trigger_type))
        finally:
            loop.close()
    except Exception as e:
        log_error(f"[sync-scheduler] Failed: {e}")
        return {"status": "failed", "error": str(e)}
