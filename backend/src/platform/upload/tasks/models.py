"""
ETL Task Models

Data models for ETL tasks, backed by the `uploads` table.
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ETLTaskStatus(str, Enum):
    """Status of an ETL task (maps to uploads.status column)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    # Fine-grained sub-statuses kept for backward compatibility with job code.
    # These are persisted as "running" in the DB status column, with the
    # fine-grained value stored in config._substatus.
    MINERU_PARSING = "mineru_parsing"
    LLM_PROCESSING = "llm_processing"


# Statuses that map to "running" in the DB
_RUNNING_SUBSTATUS = {ETLTaskStatus.MINERU_PARSING, ETLTaskStatus.LLM_PROCESSING}


class ETLTaskResult(BaseModel):
    """Result of an ETL task."""

    output_path: str = Field(..., description="Path to output JSON file in S3")
    output_size: int = Field(..., description="Size of output file in bytes")
    processing_time: float = Field(..., description="Total processing time in seconds")
    mineru_task_id: str | None = Field(None, description="MineRU task ID")


class ETLTask(BaseModel):
    """ETL task model — maps to the `uploads` table."""

    task_id: str | None = Field(
        None, description="Unique task identifier (TEXT UUID, None for new tasks)"
    )
    created_by: str | None = Field(None, description="User ID who created the task (audit field)")
    project_id: str = Field(..., description="Project ID (UUID)")
    path: str | None = Field(
        None, description="Associated version path"
    )
    type: str = Field(
        default="file_ocr",
        description="Upload type: 'file_ocr' | 'file_postprocess'",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Task configuration JSONB (stores rule_id, filename, metadata, etc.)",
    )
    status: ETLTaskStatus = Field(
        default=ETLTaskStatus.PENDING, description="Task status"
    )
    progress: int = Field(default=0, description="Progress percentage (0-100)")
    message: str | None = Field(None, description="Status message")
    error: str | None = Field(None, description="Error message if failed")
    result_path: str | None = Field(
        None, description="Result version path"
    )
    result: ETLTaskResult | None = Field(
        None, description="Task result if completed"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = Field(
        None, description="When task started running"
    )
    completed_at: datetime | None = Field(
        None, description="When task completed"
    )

    # ── Backward-compatible convenience fields ──
    # Stored inside the `config` JSONB column, not as separate DB columns.
    filename: str = Field(default="", description="Original filename (stored in config)")
    rule_id: int = Field(default=0, description="Rule ID to apply (stored in config)")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata (stored in config)"
    )

    def update_status(self, status: ETLTaskStatus, progress: int | None = None):
        """Update task status and timestamp."""
        self.status = status
        if progress is not None:
            self.progress = progress
        if (status in _RUNNING_SUBSTATUS or status == ETLTaskStatus.RUNNING) and self.started_at is None:
            self.started_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)

    def mark_failed(self, error: str):
        """Mark task as failed with error message."""
        self.status = ETLTaskStatus.FAILED
        self.error = error
        self.progress = 0
        self.updated_at = datetime.now(UTC)

    def mark_cancelled(self, reason: str | None = None):
        """Mark task as cancelled."""
        self.status = ETLTaskStatus.CANCELLED
        if reason:
            self.error = reason
        self.updated_at = datetime.now(UTC)

    def mark_completed(self, result: ETLTaskResult):
        """Mark task as completed with result."""
        self.status = ETLTaskStatus.COMPLETED
        self.result = result
        self.progress = 100
        now = datetime.now(UTC)
        self.updated_at = now
        self.completed_at = now

    def to_dict(self) -> dict[str, Any]:
        """Convert task to dictionary for database storage (uploads table)."""
        config = {**self.config}
        if self.filename:
            config["filename"] = self.filename
        if self.rule_id:
            config["rule_id"] = self.rule_id
        if self.metadata:
            config["metadata"] = self.metadata

        # Map fine-grained sub-statuses to DB "running" and preserve detail
        if self.status in _RUNNING_SUBSTATUS:
            db_status = "running"
            config["_substatus"] = self.status.value
        else:
            db_status = self.status.value
            config.pop("_substatus", None)

        data: dict[str, Any] = {
            "created_by": self.created_by,
            "project_id": self.project_id,
            "path": self.path,
            "type": self.type,
            "config": config,
            "status": db_status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "result_path": self.result_path,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

        if self.task_id is not None:
            data["id"] = self.task_id

        if self.result:
            data["result"] = self.result.model_dump()

        if self.started_at:
            data["started_at"] = self.started_at.isoformat()
        if self.completed_at:
            data["completed_at"] = self.completed_at.isoformat()

        return data

    @classmethod
    def _parse_timestamp(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ETLTask":
        """Create task from database record (uploads table)."""
        result = None
        if data.get("result"):
            result = ETLTaskResult(**data["result"])

        config = data.get("config") or {}

        created_at = cls._parse_timestamp(data.get("created_at")) or datetime.now(UTC)
        updated_at = cls._parse_timestamp(data.get("updated_at")) or datetime.now(UTC)
        started_at = cls._parse_timestamp(data.get("started_at"))
        completed_at = cls._parse_timestamp(data.get("completed_at"))

        # Restore fine-grained sub-status from config if available
        substatus = config.get("_substatus")
        if substatus:
            try:
                status = ETLTaskStatus(substatus)
            except ValueError:
                status = ETLTaskStatus(data["status"])
        else:
            status = ETLTaskStatus(data["status"])

        return cls(
            task_id=data.get("id"),
            created_by=data.get("created_by") or data.get("user_id"),
            project_id=data["project_id"],
            path=data.get("path"),
            type=data.get("type", "file_ocr"),
            config=config,
            status=status,
            progress=data.get("progress", 0),
            message=data.get("message"),
            error=data.get("error"),
            result_path=data.get("result_path"),
            result=result,
            created_at=created_at,
            updated_at=updated_at,
            started_at=started_at,
            completed_at=completed_at,
            filename=config.get("filename", ""),
            rule_id=config.get("rule_id", 0),
            metadata=config.get("metadata", {}),
        )
