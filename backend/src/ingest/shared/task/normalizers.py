"""
Normalizers - Convert ETL tasks to unified format.

SaaS normalizers removed — SaaS imports now go through ImportJob or SynchronizeEngine.
"""


from src.infra.file_formats import detect_ingest_type as detect_file_ingest_type
from src.infra.file_processing.tasks.models import ETLTask, ETLTaskStatus
from src.ingest.schemas import (
    IngestStatus,
    IngestTaskResponse,
    IngestType,
    SourceType,
)

# Re-exported under the legacy name for backward compatibility with
# call sites that import `detect_file_ingest_type` from here. The
# canonical implementation lives in `src.infra.file_formats`.
__all__ = [
    "detect_file_ingest_type",
    "normalize_file_status",
    "normalize_file_task",
]


def normalize_file_status(status: ETLTaskStatus) -> IngestStatus:
    """ETL status -> Unified status."""
    mapping = {
        ETLTaskStatus.PENDING: IngestStatus.PENDING,
        # ``RUNNING`` is the generic "worker has it" status used by the
        # finalize-upload job (direct-to-S3 path). MineRU/LLM keep their
        # fine-grained sub-statuses for OCR pipeline observability.
        ETLTaskStatus.RUNNING: IngestStatus.PROCESSING,
        ETLTaskStatus.MINERU_PARSING: IngestStatus.PROCESSING,
        ETLTaskStatus.LLM_PROCESSING: IngestStatus.PROCESSING,
        ETLTaskStatus.COMPLETED: IngestStatus.COMPLETED,
        ETLTaskStatus.FAILED: IngestStatus.FAILED,
        ETLTaskStatus.CANCELLED: IngestStatus.CANCELLED,
    }
    return mapping.get(status, IngestStatus.PENDING)


def normalize_file_task(task: ETLTask) -> IngestTaskResponse:
    """ETL task -> Unified response."""
    ingest_type = detect_file_ingest_type(task.filename)

    content_path = None
    if task.result and hasattr(task.result, "content_path"):
        content_path = task.result.content_path
    elif task.metadata.get("content_path"):
        content_path = task.metadata.get("content_path")
    elif task.metadata.get("mount_path"):
        content_path = task.metadata.get("mount_path")

    return IngestTaskResponse(
        task_id=str(task.task_id),
        source_type=SourceType.FILE,
        ingest_type=ingest_type,
        status=normalize_file_status(task.status),
        progress=task.progress,
        message=task.metadata.get("message"),
        content_path=content_path,
        error=task.error,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.updated_at if task.status == ETLTaskStatus.COMPLETED else None,
        filename=task.filename,
        metadata=task.metadata,
    )
