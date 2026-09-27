"""Scheduled recovery for stale Integration sync runs."""

from __future__ import annotations

from src.config import settings
from src.platform.synchronize.run_repository import SyncRunRepository
from src.infra.supabase.client import SupabaseClient
from src.utils.logger import log_error


async def process_sync_run_reaper() -> dict:
    try:
        repo = SyncRunRepository(SupabaseClient())
        recovered = repo.recover_stale_active_runs(
            lease_seconds=settings.SYNC_RUN_LEASE_SECONDS,
            limit=settings.SYNC_RUN_REAPER_MAX_PER_RUN,
        )
        return {
            "status": "ok",
            "recovered": len(recovered),
            "run_ids": [run.id for run in recovered],
        }
    except Exception as exc:  # noqa: BLE001
        log_error(f"[sync-run-reaper] scheduler job failed: {exc}")
        return {"status": "failed", "error": str(exc)}
