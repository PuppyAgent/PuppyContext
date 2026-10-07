"""
Ingest Gateway Service - Routes task status queries.

File uploads use the ETL service. SaaS/URL imports use durable ImportJob rows.
"""

import asyncio
import logging

from fastapi import HTTPException
from src.exceptions import AppException
from src.platform.imports.service import ImportJobService
from src.platform.imports.dependencies import get_import_arq_client

from src.ingest.schemas import (
    IngestStatus,
    IngestTaskResponse,
    IngestType,
    SourceType,
)
from src.ingest.shared.task.normalizers import (
    normalize_file_task,
)
from src.platform.imports.provider import detect_import_provider
from src.platform.imports.repository import ImportJobRepository
from src.platform.imports.schemas import ImportJobStatus
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService

logger = logging.getLogger(__name__)


class IngestService:
    """Unified task status service for file and one-time import tasks."""

    def __init__(self, file_service, authorization: AuthorizationService):
        self.file_service = file_service
        self.authorization = authorization
        self._import_job_repo: ImportJobRepository | None = None

    @property
    def import_job_repo(self) -> ImportJobRepository:
        if self._import_job_repo is None:
            self._import_job_repo = ImportJobRepository()
        return self._import_job_repo

    async def get_task(
        self,
        task_id: str,
        source_type: SourceType,
        user_id: str,
    ) -> IngestTaskResponse | None:
        """Get task status."""
        if source_type != SourceType.FILE:
            return self._get_import_task(task_id, user_id)

        # ``task_id`` from the DB is a UUID string (uploads.id is TEXT).
        # The previous ``int(task_id)`` cast was a holdover from the
        # bigint-ID schema and crashed on UUIDs — which never showed
        # up in practice because raw uploads were marked COMPLETED
        # synchronously and clients never polled. With direct-to-S3
        # uploads polling is now the norm, so the cast has to go.
        task = await self.file_service.get_task_status_with_access_check(
            task_id=task_id,
            user_id=user_id,
        )
        return normalize_file_task(task) if task else None

    async def batch_get_tasks(
        self,
        tasks: list[dict],
        user_id: str,
    ) -> list[IngestTaskResponse]:
        """Batch query file/import tasks."""
        file_tasks = [t for t in tasks if t.get("source_type") == SourceType.FILE.value]
        import_tasks = [t for t in tasks if t.get("source_type") != SourceType.FILE.value]

        results = []
        if file_tasks:
            file_results = await asyncio.gather(*[
                self.get_task(t["task_id"], SourceType.FILE, user_id)
                for t in file_tasks
            ], return_exceptions=True)
            results.extend([r for r in file_results if r and not isinstance(r, Exception)])
        if import_tasks:
            import_results = [
                self._get_import_task(t["task_id"], user_id)
                for t in import_tasks
            ]
            results.extend([r for r in import_results if r])

        return results

    async def cancel_task(
        self,
        task_id: str,
        source_type: SourceType,
        user_id: str,
    ) -> bool:
        """Cancel a task."""
        if source_type != SourceType.FILE:
            service = ImportJobService(
                repo=self.import_job_repo,
                authorization=self.authorization,
                arq_client=get_import_arq_client(),
            )
            try:
                service.cancel(task_id, user_id)
            except (HTTPException, AppException) as exc:
                if exc.status_code in {403, 404}:
                    return False
                raise
            return True
        try:
            # See note in ``get_task``: task_id is a UUID string, not an int.
            task = await self.file_service.cancel_task(
                task_id=task_id,
                user_id=user_id,
            )
            return task is not None
        except Exception as e:
            logger.error(f"Failed to cancel task {task_id}: {e}")
            return False

    def _can_access_project(
        self, project_id: str, user_id: str, action: ProjectAction
    ) -> bool:
        return self.authorization.allows(project_id, user_id, action)

    def _get_import_task(self, task_id: str, user_id: str) -> IngestTaskResponse | None:
        job = self.import_job_repo.get(task_id)
        if not job or not self._can_access_project(
            job.project_id, user_id, ProjectAction.CONTENT_READ
        ):
            return None

        status_map = {
            ImportJobStatus.QUEUED.value: IngestStatus.PENDING,
            ImportJobStatus.RUNNING.value: IngestStatus.PROCESSING,
            ImportJobStatus.COMPLETED.value: IngestStatus.COMPLETED,
            ImportJobStatus.FAILED.value: IngestStatus.FAILED,
            ImportJobStatus.CANCELLED.value: IngestStatus.CANCELLED,
        }
        provider = job.provider or detect_import_provider(job.source_url)

        return IngestTaskResponse(
            task_id=job.id,
            source_type=SourceType.SAAS if provider != "url" else SourceType.URL,
            ingest_type=_provider_to_ingest_type(provider),
            status=status_map.get(job.status, IngestStatus.PENDING),
            progress=job.progress,
            message=job.message,
            content_path=job.result_path,
            error=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
            filename=job.name,
            metadata={
                "provider": provider,
                "source_url": job.source_url,
                "phase": job.phase,
                "result_commit_id": job.result_commit_id,
            },
        )


def _provider_to_ingest_type(provider: str) -> IngestType:
    mapping = {
        "github": IngestType.GITHUB,
        "notion": IngestType.NOTION,
        "gmail": IngestType.GMAIL,
        "google_drive": IngestType.GOOGLE_DRIVE,
        "google_sheets": IngestType.GOOGLE_SHEETS,
        "google_docs": IngestType.GOOGLE_DOCS,
        "google_calendar": IngestType.GOOGLE_CALENDAR,
        "airtable": IngestType.AIRTABLE,
        "linear": IngestType.LINEAR,
        "url": IngestType.WEB_PAGE,
    }
    return mapping.get(provider, IngestType.WEB_PAGE)
