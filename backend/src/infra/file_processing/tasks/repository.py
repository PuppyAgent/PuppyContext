"""
ETL Task Repository

Repository for managing ETL task persistence in the `uploads` table.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from src.infra.supabase.client import SupabaseClient
from src.infra.supabase.exceptions import handle_supabase_error
from src.infra.file_processing.tasks.models import ETLTask, ETLTaskStatus

logger = logging.getLogger(__name__)

ETL_UPLOAD_TYPES = ["file_ocr", "file_postprocess"]


class ETLTaskRepositoryBase(ABC):
    """Abstract base class for ETL task repository."""

    @abstractmethod
    def create_task(self, task: ETLTask) -> ETLTask:
        """
        Create a new task in storage.

        Args:
            task: Task to create (task_id will be assigned if not set)

        Returns:
            Task with assigned task_id
        """

    @abstractmethod
    def get_task(self, task_id: str) -> ETLTask | None:
        """
        Get task by ID.

        Args:
            task_id: Task identifier (UUID text)

        Returns:
            Task if found, None otherwise
        """

    @abstractmethod
    def update_task(self, task: ETLTask) -> ETLTask | None:
        """
        Update existing task.

        Args:
            task: Task to update

        Returns:
            Updated task if found, None otherwise
        """

    @abstractmethod
    def list_tasks(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ETLTask]:
        """
        List tasks with optional filters.

        Args:
            project_id: Filter by project ID
            status: Filter by status
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of tasks
        """

    def list_tasks_strict(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ETLTask]:
        """List tasks, surfacing storage failure to strict cleanup callers."""

        return self.list_tasks(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @abstractmethod
    def count_tasks(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
    ) -> int:
        """
        Count tasks with optional filters.

        Args:
            project_id: Filter by project ID
            status: Filter by status

        Returns:
            Number of matching tasks
        """

    @abstractmethod
    def delete_task(self, task_id: str) -> bool:
        """
        Delete task by ID.

        Args:
            task_id: Task identifier (UUID text)

        Returns:
            True if deleted, False if not found
        """


class ETLTaskRepositorySupabase(ETLTaskRepositoryBase):
    """Supabase implementation of ETL task repository using the `uploads` table."""

    TABLE_NAME = "uploads"

    def __init__(self):
        """Initialize Supabase task repository."""
        self.supabase = SupabaseClient().client
        logger.info("ETLTaskRepositorySupabase initialized")

    def create_task(self, task: ETLTask) -> ETLTask:
        """Create a new task in Supabase."""
        try:
            insert_data = task.to_dict()

            # Generate a UUID if not already set (uploads.id is TEXT PK)
            if "id" not in insert_data or not insert_data["id"]:
                insert_data["id"] = str(uuid.uuid4())

            response = self.supabase.table(self.TABLE_NAME).insert(insert_data).execute()

            if not response.data or len(response.data) == 0:
                raise Exception("Failed to create task: no data returned")

            row = response.data[0]
            created_task = ETLTask.from_dict(row)

            logger.info(
                f"Created task: {created_task.task_id} for created_by {created_task.created_by}"
            )
            return created_task

        except Exception as e:
            # ``handle_supabase_error`` returns a SupabaseException; we
            # MUST raise it. The previous fall-through made this method
            # silently return ``None`` on failure (e.g. CHECK constraint
            # violations), which then exploded at the call site as
            # ``AttributeError: 'NoneType' object has no attribute ...``
            # — a confusing diagnostic that hid the real cause.
            raise handle_supabase_error(e, "create ETL task") from e

    def get_task(self, task_id: str) -> ETLTask | None:
        """Get task by ID from Supabase."""
        try:
            response = (
                self.supabase.table(self.TABLE_NAME)
                .select("*")
                .eq("id", task_id)
                .in_("type", ETL_UPLOAD_TYPES)
                .execute()
            )

            if not response.data or len(response.data) == 0:
                logger.debug(f"Task not found: {task_id}")
                return None

            row = response.data[0]
            return ETLTask.from_dict(row)

        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}")
            return None

    def update_task(self, task: ETLTask) -> ETLTask | None:
        """Update existing task in Supabase."""
        if task.task_id is None:
            logger.error("Cannot update task without task_id")
            return None

        try:
            update_data = task.to_dict()

            if "id" in update_data:
                del update_data["id"]

            update_data["updated_at"] = datetime.now(UTC).isoformat()

            response = (
                self.supabase.table(self.TABLE_NAME)
                .update(update_data)
                .eq("id", task.task_id)
                .execute()
            )

            if not response.data or len(response.data) == 0:
                logger.warning(f"Task not found for update: {task.task_id}")
                return None

            row = response.data[0]
            updated_task = ETLTask.from_dict(row)

            logger.info(f"Updated task: {task.task_id}")
            return updated_task

        except Exception as e:
            raise handle_supabase_error(e, "update ETL task") from e

    def list_tasks(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ETLTask]:
        """List tasks with optional filters (only ETL upload types)."""
        try:
            return self.list_tasks_strict(
                project_id=project_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            logger.error(f"Error listing tasks: {e}")
            return []

    def list_tasks_strict(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ETLTask]:
        """List ETL tasks without converting repository failure to emptiness."""

        try:
            query = self.supabase.table(self.TABLE_NAME).select("*")
            query = query.in_("type", ETL_UPLOAD_TYPES)
            if project_id is not None:
                query = query.eq("project_id", project_id)
            if status is not None:
                query = query.eq("status", status.value)
            query = query.range(offset, offset + limit - 1).order("created_at", desc=True)
            response = query.execute()
            tasks = [ETLTask.from_dict(row) for row in response.data]
            logger.info(
                f"Listed {len(tasks)} tasks "
                f"(project_id={project_id}, status={status}, "
                f"offset={offset}, limit={limit})"
            )
            return tasks
        except Exception as e:
            raise handle_supabase_error(e, "list ETL tasks") from e

    def count_tasks(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
    ) -> int:
        """Count tasks with optional filters (only ETL upload types)."""
        try:
            query = self.supabase.table(self.TABLE_NAME).select("id", count="exact")

            query = query.in_("type", ETL_UPLOAD_TYPES)

            # Use project_id for counting (created_by is audit-only)
            if project_id is not None:
                query = query.eq("project_id", project_id)
            if status is not None:
                query = query.eq("status", status.value)

            response = query.execute()

            count = response.count if response.count is not None else 0
            return count

        except Exception as e:
            logger.error(f"Error counting tasks: {e}")
            return 0

    def delete_task(self, task_id: str) -> bool:
        """Delete task by ID."""
        try:
            response = self.supabase.table(self.TABLE_NAME).delete().eq("id", task_id).execute()

            if not response.data or len(response.data) == 0:
                logger.warning(f"Task not found for deletion: {task_id}")
                return False

            logger.info(f"Deleted task: {task_id}")
            return True

        except Exception as e:
            raise handle_supabase_error(e, "delete ETL task") from e
