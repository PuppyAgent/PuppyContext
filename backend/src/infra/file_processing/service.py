"""
ETL Service

ETL control-plane service (API-side).

Execution happens in ARQ workers; API process is responsible for:
- creating task records (Supabase)
- maintaining runtime state (Redis)
- enqueueing jobs (ARQ)
- aggregating status (Redis first, fallback Supabase)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.exceptions import ErrorCode, NotFoundException
from src.infra.supabase.dependencies import get_supabase_client
from src.infra.file_processing.arq_client import ETLArqClient
from src.infra.file_processing.config import etl_config
from src.infra.file_processing.exceptions import RuleNotFoundError
from src.infra.file_processing.rules.default_rules import get_default_rule_id
from src.infra.file_processing.rules.repository_supabase import RuleRepositorySupabase
from src.infra.file_processing.state.models import ETLPhase, ETLRuntimeState
from src.infra.file_processing.state.repository import ETLStateRepositoryRedis
from src.infra.file_processing.tasks.models import ETLTask, ETLTaskStatus
from src.infra.file_processing.tasks.repository import ETLTaskRepositoryBase

logger = logging.getLogger(__name__)


class ETLService:
    """Core ETL service for document processing."""

    def __init__(
        self,
        task_repository: ETLTaskRepositoryBase,
        arq_client: ETLArqClient,
        state_repo: ETLStateRepositoryRedis,
    ):
        """
        Initialize ETL service.

        Args:
            task_repository: Repository for ETL tasks (Supabase)
            arq_client: ARQ client for enqueueing jobs
            state_repo: Redis runtime state repository
        """
        self.task_repository = task_repository
        self.arq_client = arq_client
        self.state_repo = state_repo

        logger.info("ETLService initialized")

    def _get_rule_repository(self, org_id: str | None = None, created_by: str | None = None) -> RuleRepositorySupabase:
        """
        Get rule repository instance.

        Args:
            org_id: Organization ID (for filtering)
            created_by: Creator user ID

        Returns:
            RuleRepositorySupabase instance
        """
        supabase_client = get_supabase_client()
        return RuleRepositorySupabase(supabase_client=supabase_client, org_id=org_id, created_by=created_by)

    async def start(self):
        """Warm up control-plane dependencies."""
        await self.arq_client.get_pool()
        logger.info("ETL service started (control-plane)")

    async def stop(self):
        """No-op in API process (worker is separate)."""
        logger.info("ETL service stopped (control-plane)")

    async def submit_etl_task(
        self,
        user_id: str,
        project_id: str,
        filename: str,
        rule_id: int | None,
        s3_key: str | None = None,
    ) -> ETLTask:
        """
        Submit an ETL task to the queue.

        Args:
            user_id: User ID (string type)
            project_id: Project ID
            filename: Source filename
            rule_id: Rule ID to apply

        Returns:
            Created ETL task

        Raises:
            RuleNotFoundError: If rule doesn't exist
        """
        # Determine rule: use global default if omitted
        if rule_id is None:
            rule_repository = self._get_rule_repository(created_by=user_id)
            rule_id = get_default_rule_id(rule_repository)

        rule_repository = self._get_rule_repository(created_by=user_id)
        rule = rule_repository.get_rule(str(rule_id))
        if not rule:
            logger.error(f"Rule not found: {rule_id}")
            raise RuleNotFoundError(str(rule_id))

        # Create task (task_id will be assigned by repository)
        task = ETLTask(
            task_id=None,  # Will be assigned by database
            created_by=user_id,
            project_id=project_id,
            filename=filename,
            rule_id=int(rule_id),
        )

        if s3_key:
            task.metadata["s3_key"] = s3_key

        # Create in DB to assign task_id
        task_with_id = self.task_repository.create_task(task)

        # Init runtime state and enqueue OCR job
        job_id = await self.arq_client.enqueue_ocr(task_with_id.task_id)
        state = ETLRuntimeState(
            task_id=task_with_id.task_id,
            user_id=task_with_id.created_by or task_with_id.project_id,
            project_id=task_with_id.project_id,
            filename=task_with_id.filename,
            rule_id=task_with_id.rule_id,
            status=ETLTaskStatus.PENDING,
            phase=ETLPhase.OCR,
            progress=0,
            arq_job_id_ocr=job_id,
            metadata=task_with_id.metadata,
        )
        await self.state_repo.set(state)

        logger.info(
            f"ETL task submitted: task_id={task_with_id.task_id}, "
            f"user_id={user_id}, filename={filename}, rule_id={rule_id}"
        )

        return task_with_id

    def get_default_rule_id_for_user(self, user_id: str) -> int:
        """Resolve the default rule id for a user, in one Supabase query.

        Hoisted so callers that create N tasks for the same user
        (e.g. /upload/init for a multi-file folder upload) can do
        the lookup ONCE and pass the result into N
        ``create_pending_upload_task`` calls — saving N-1 etl_rules
        list queries (~400ms each) per request.
        """
        rule_repository = self._get_rule_repository(created_by=user_id)
        return int(get_default_rule_id(rule_repository))

    def create_pending_upload_task(
        self,
        *,
        user_id: str,
        project_id: str,
        filename: str,
        s3_key: str,
        upload_id: str,
        mount_path: str,
        size: int,
        content_type: str | None,
        chunk_size: int,
        total_parts: int,
        max_single_file_bytes: int | None,
        default_rule_id: int | None = None,
        upload_job_id: str | None = None,
    ) -> ETLTask:
        """
        Create a ``pending`` task for a direct-to-S3 multipart upload.

        Lifecycle:
          1. Frontend calls ``/upload/init`` -> we create this task and
             return presigned PUT URLs alongside its ``task_id``.
          2. Frontend uploads each part directly to S3.
          3. Frontend calls ``/upload/complete`` -> we run
             ``CompleteMultipartUpload`` and enqueue
             ``etl_finalize_upload_job`` which writes the bytes into Version Engine.
          4. Worker marks the task COMPLETED (or FAILED on mount
             error). The frontend's existing ``BackgroundTaskNotifier``
             polling picks up the terminal state.

        Stored as ``type="file_postprocess"`` (one of the four values
        the ``chk_uploads_type`` CHECK constraint allows). We
        distinguish these tasks from legacy postprocess work via
        ``metadata.upload_kind == "direct_s3_multipart"``; downstream
        worker dispatch is by job-name (``etl_finalize_upload_job``)
        not by ``type``, so reusing the existing enum value is safe.
        Once a migration extends the constraint we can rename this
        to a dedicated ``file_upload`` type.
        """
        # ``rule_id`` is non-null in the legacy ``uploads`` schema, so
        # we attach the global default for compatibility — the
        # finalize worker never invokes the rule engine.
        if default_rule_id is None:
            rule_repository = self._get_rule_repository(created_by=user_id)
            default_rule_id = int(get_default_rule_id(rule_repository))

        task = ETLTask(
            task_id=None,
            created_by=user_id,
            project_id=project_id,
            filename=filename,
            rule_id=int(default_rule_id),
            path=mount_path,
            type="file_postprocess",
            status=ETLTaskStatus.PENDING,
            progress=0,
            metadata={
                "s3_key": s3_key,
                "upload_id": upload_id,
                "mount_path": mount_path,
                "size": size,
                "chunk_size": chunk_size,
                "total_parts": total_parts,
                "max_single_file_bytes": max_single_file_bytes,
                "content_type": content_type,
                "upload_kind": "direct_s3_multipart",
                "upload_job_id": upload_job_id,
            },
        )

        return self.task_repository.create_task(task)

    def create_failed_task(
        self,
        *,
        user_id: str,
        project_id: str,
        filename: str,
        rule_id: int | None,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> ETLTask:
        """
        Create a failed ETL task record for cases where we want a pollable task_id
        but the pipeline cannot be started (e.g. upload failed).
        """
        if rule_id is None:
            rule_repository = self._get_rule_repository(created_by=user_id)
            rule_id = get_default_rule_id(rule_repository)

        rule_repository = self._get_rule_repository(created_by=user_id)
        rule = rule_repository.get_rule(str(rule_id))
        if not rule:
            logger.error(f"Rule not found: {rule_id}")
            raise RuleNotFoundError(str(rule_id))

        task = ETLTask(
            task_id=None,
            created_by=user_id,
            project_id=project_id,
            filename=filename,
            rule_id=int(rule_id),
            status=ETLTaskStatus.FAILED,
            progress=0,
            error=error,
            metadata=metadata or {},
        )

        task_with_id = self.task_repository.create_task(task)
        logger.info(
            f"Created failed ETL task: task_id={task_with_id.task_id}, user_id={user_id}, error={error}"
        )
        return task_with_id

    async def get_task_status(self, task_id: str | int) -> ETLTask | None:
        """
        Get ETL task status.

        Args:
            task_id: Task ID

        Returns:
            ETLTask if found, None otherwise
        """
        state = await self.state_repo.get(task_id)
        if state is not None:
            # Reconcile "stuck running" tasks (e.g. worker crash/timeout before state could be finalized).
            # If the runtime state hasn't been updated for longer than job_timeout + buffer, mark it failed.
            if state.status in (
                ETLTaskStatus.MINERU_PARSING,
                ETLTaskStatus.LLM_PROCESSING,
            ):
                age_s = (datetime.now(UTC) - state.updated_at).total_seconds()
                if age_s > (etl_config.etl_task_timeout + 30):
                    err = f"Runtime state stale for {int(age_s)}s (timeout={etl_config.etl_task_timeout}s)"
                    try:
                        # Best-effort: mark Redis terminal state
                        state.status = ETLTaskStatus.FAILED
                        state.phase = ETLPhase.FINALIZE
                        state.progress = 0
                        state.error_stage = "stale"
                        state.error_message = err
                        await self.state_repo.set_terminal(state)
                    except Exception:
                        logger.warning(
                            f"Failed to set stale terminal state for task_id={task_id}",
                            exc_info=True,
                        )

                    try:
                        # Best-effort: mark DB terminal state
                        task = self.task_repository.get_task(task_id)
                        if task:
                            task.status = ETLTaskStatus.FAILED
                            task.error = err
                            task.metadata["error_stage"] = "stale"
                            self.task_repository.update_task(task)
                            return task
                    except Exception:
                        logger.warning(
                            f"Failed to persist stale failure for task_id={task_id}",
                            exc_info=True,
                        )

            # Terminal state details should come from DB for result payload stability
            if state.status in (
                ETLTaskStatus.COMPLETED,
                ETLTaskStatus.FAILED,
                ETLTaskStatus.CANCELLED,
            ):
                return self.task_repository.get_task(task_id)

            return ETLTask(
                task_id=state.task_id,
                created_by=state.user_id,
                project_id=state.project_id,
                filename=state.filename,
                rule_id=state.rule_id,
                status=state.status,
                progress=state.progress,
                created_at=state.created_at.replace(tzinfo=None),
                updated_at=state.updated_at.replace(tzinfo=None),
                error=state.error_message,
                metadata=state.metadata,
            )

        return self.task_repository.get_task(task_id)

    async def get_task_status_with_access_check(
        self, task_id: str | int, user_id: str
    ) -> ETLTask:
        """
        Get task status and verify user permissions.

        Args:
            task_id: Task ID
            user_id: User ID (string type)

        Returns:
            Verified ETLTask object

        Raises:
            NotFoundException: If task does not exist or user has no permission
        """
        task = await self.get_task_status(task_id)
        if not task:
            raise NotFoundException(
                f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
            )

        # Check user permissions (created_by is audit field; when set, must match)
        if task.created_by is not None and task.created_by != user_id:
            raise NotFoundException(
                f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
            )

        return task

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
    ) -> list[ETLTask]:
        """
        List ETL tasks with optional filters.

        Args:
            project_id: Filter by project ID
            status: Filter by status

        Returns:
            List of matching tasks
        """
        # Use project_id for listing (created_by is audit-only)
        tasks = self.task_repository.list_tasks(
            project_id=project_id, status=status, limit=100, offset=0
        )

        for t in tasks:
            if t.task_id is None:
                continue
            st = await self.state_repo.get(t.task_id)
            if not st:
                continue
            if st.status in (
                ETLTaskStatus.PENDING,
                ETLTaskStatus.MINERU_PARSING,
                ETLTaskStatus.LLM_PROCESSING,
            ):
                t.status = st.status
                t.progress = st.progress
                t.error = st.error_message or t.error
                merged = dict(t.metadata or {})
                merged.update(st.metadata or {})
                t.metadata = merged

        return tasks

    @staticmethod
    def _validate_cancellable(status: ETLTaskStatus, *, force: bool) -> None:
        """Raise ValueError if the task/state status is not cancellable."""
        terminal = {ETLTaskStatus.COMPLETED, ETLTaskStatus.FAILED, ETLTaskStatus.CANCELLED}
        if not force and status != ETLTaskStatus.PENDING:
            raise ValueError(f"Task not cancellable in status={status.value}")
        if force and status in terminal:
            raise ValueError(f"Task not cancellable in status={status.value}")

    async def cancel_task(
        self, task_id: str | int, user_id: str, *, force: bool = False
    ) -> ETLTask:
        """
        Cancel a queued/pending task.

        By default, only allowed when Redis runtime state is still `pending` (i.e. not started).
        When force=True, allow cancelling running tasks by marking terminal CANCELLED state.
        Note: this cannot interrupt external providers immediately; it is a control-plane cancellation.
        """
        task = self.task_repository.get_task(task_id)
        if not task:
            raise NotFoundException(
                f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
            )
        if task.created_by is not None and task.created_by != user_id:
            raise NotFoundException(
                f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
            )

        self._validate_cancellable(task.status, force=force)

        state = await self.state_repo.get(task_id)
        if state is None:
            # Redis state may have expired; create a minimal terminal state so worker jobs can honor cancellation
            state = ETLRuntimeState(
                task_id=task_id,
                user_id=task.created_by or task.project_id,
                project_id=task.project_id,
                filename=task.filename,
                rule_id=task.rule_id,
                status=ETLTaskStatus.CANCELLED,
                phase=ETLPhase.FINALIZE,
                progress=0,
                error_message="Cancelled by user",
                metadata=task.metadata,
            )
        else:
            if task.created_by is not None and state.user_id != user_id:
                raise NotFoundException(
                    f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
                )
            self._validate_cancellable(state.status, force=force)
            state.status = ETLTaskStatus.CANCELLED
            state.phase = ETLPhase.FINALIZE
            state.progress = 0
            state.error_message = "Cancelled by user"

        await self.state_repo.set_terminal(state)

        task.mark_cancelled("Cancelled by user")
        self.task_repository.update_task(task)
        return task

    async def retry_task(self, task_id: str | int, user_id: str, from_stage: str) -> ETLTask:
        """
        Retry from a given stage: "mineru" or "postprocess".
        """
        task = self.task_repository.get_task(task_id)
        if not task:
            raise NotFoundException(
                f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
            )
        if task.created_by is not None and task.created_by != user_id:
            raise NotFoundException(
                f"ETL task not found: {task_id}", code=ErrorCode.NOT_FOUND
            )

        state = await self.state_repo.get(task_id)
        if state is None:
            state = ETLRuntimeState(
                task_id=task_id,
                user_id=task.created_by or task.project_id,
                project_id=task.project_id,
                filename=task.filename,
                rule_id=task.rule_id,
                status=ETLTaskStatus.PENDING,
                phase=ETLPhase.OCR,
                progress=0,
                metadata=task.metadata,
            )

        if state.status in (ETLTaskStatus.MINERU_PARSING, ETLTaskStatus.LLM_PROCESSING):
            raise ValueError(f"Task is running (status={state.status.value})")

        if from_stage == "postprocess":
            md_key = state.artifact_mineru_markdown_key or task.metadata.get(
                "artifact_mineru_markdown_key"
            )
            if not md_key:
                raise ValueError(
                    "Cannot retry postprocess: missing markdown artifact pointer"
                )
            job_id = await self.arq_client.enqueue_postprocess(task_id)
            state.phase = ETLPhase.POSTPROCESS
            state.status = ETLTaskStatus.LLM_PROCESSING
            state.progress = 60
            state.arq_job_id_postprocess = job_id
            state.error_message = None
            state.error_stage = None
            state.artifact_mineru_markdown_key = md_key
            await self.state_repo.set(state)
        elif from_stage == "mineru":
            job_id = await self.arq_client.enqueue_ocr(task_id)
            state.phase = ETLPhase.OCR
            state.status = ETLTaskStatus.PENDING
            state.progress = 0
            state.arq_job_id_ocr = job_id
            state.arq_job_id_postprocess = None
            state.artifact_mineru_markdown_key = None
            state.provider_task_id = None
            state.error_message = None
            state.error_stage = None
            await self.state_repo.set(state)
        else:
            raise ValueError("from_stage must be 'mineru' or 'postprocess'")

        # Mark DB status back to pending for visibility (best effort)
        task.status = ETLTaskStatus.PENDING
        task.error = None
        self.task_repository.update_task(task)
        return task

    def get_queue_size(self) -> int:
        """Queue is managed by ARQ; API does not track it."""
        return 0

    def get_task_count(self) -> int:
        """Task count is stored in DB; API does not track it."""
        return 0
