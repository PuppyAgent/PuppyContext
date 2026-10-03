"""Scheduled recovery for stale upload jobs.

Upload finalize runs inline in the API request (not on a worker), so an
`upload_jobs` row only stays `running` forever if the API process died
mid-finalize. A legitimate finalize cannot outlast the HTTP request, so a
`running` row older than the stale window (set well above any request lifetime)
is a dead-process orphan we fail so it stops looking active.
"""

from __future__ import annotations

from src.config import settings
from src.infra.supabase.client import SupabaseClient
from src.platform.upload.repository import UploadJobRepository
from src.utils.logger import log_error


async def process_upload_job_reaper() -> dict:
    try:
        repo = UploadJobRepository(SupabaseClient())
        recovered = repo.recover_stale_jobs(
            stale_seconds=settings.UPLOAD_JOB_STALE_SECONDS,
            limit=settings.UPLOAD_JOB_REAPER_MAX_PER_RUN,
        )
        return {"status": "ok", "recovered": len(recovered), "job_ids": recovered}
    except Exception as exc:  # noqa: BLE001
        log_error(f"[upload-job-reaper] scheduler job failed: {exc}")
        return {"status": "failed", "error": str(exc)}
