"""Scheduled recovery for stale one-time import jobs.

A live import cannot outlast the import worker ``job_timeout`` (ARQ cancels it
first, which marks it failed). So any QUEUED/RUNNING row still untouched after
the stale window means the worker died mid-job or the job was never consumed —
those would otherwise sit active forever. We floor the stale window above the
worker timeout so a still-live job is never reaped.
"""

from __future__ import annotations

from src.config import settings
from src.infra.supabase.client import SupabaseClient
from src.platform.imports.config import import_config
from src.platform.imports.repository import ImportJobRepository
from src.utils.logger import log_error


async def process_import_job_reaper() -> dict:
    try:
        stale = max(
            settings.IMPORT_JOB_STALE_SECONDS,
            import_config.import_task_timeout + 600,
        )
        repo = ImportJobRepository(SupabaseClient())
        recovered = repo.recover_stale_active_jobs(
            stale_seconds=stale,
            limit=settings.IMPORT_JOB_REAPER_MAX_PER_RUN,
        )
        return {
            "status": "ok",
            "recovered": len(recovered),
            "job_ids": [job.id for job in recovered],
        }
    except Exception as exc:  # noqa: BLE001
        log_error(f"[import-job-reaper] scheduler job failed: {exc}")
        return {"status": "failed", "error": str(exc)}
