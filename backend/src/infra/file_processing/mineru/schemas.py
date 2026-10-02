"""
MineRU Client Schemas

Pydantic models for MineRU API requests and responses.
"""

from enum import Enum

from pydantic import BaseModel, Field

_NO_DATA_FIELD_MSG = "No data field in response"


class MineRUModelVersion(str, Enum):
    """MineRU model versions."""

    VLM = "vlm"
    DOCUMENT = "document"
    OCR = "ocr"


class MineRUTaskState(str, Enum):
    """MineRU task states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "done"  # MineRU API uses "done" for completed state
    FAILED = "failed"
    CONVERTING = "converting"  # Format conversion in progress
    WAITING_FILE = "waiting-file"  # Waiting for file upload


class CreateTaskRequest(BaseModel):
    """Request to create a MineRU parsing task."""

    url: str = Field(..., description="Presigned URL of the file to parse")
    model_version: MineRUModelVersion = Field(
        default=MineRUModelVersion.VLM, description="Model version to use for parsing"
    )
    data_id: str | None = Field(
        None, description="Optional data identifier for tracking"
    )


class CreateTaskData(BaseModel):
    """Data field in create task response."""

    task_id: str = Field(..., description="Task ID for tracking")


class CreateTaskResponse(BaseModel):
    """Response from creating a MineRU task."""

    code: int = Field(..., description="API status code, 0 for success")
    msg: str = Field(..., description="API message")
    trace_id: str | None = Field(None, description="Trace ID for debugging")
    data: CreateTaskData | None = Field(None, description="Task data")

    @property
    def task_id(self) -> str:
        """Get task_id from data field for backward compatibility."""
        if not self.data:
            raise ValueError(_NO_DATA_FIELD_MSG)
        return self.data.task_id


class ExtractProgress(BaseModel):
    """Extract progress information."""

    extracted_pages: int | None = Field(None, description="Number of extracted pages")
    total_pages: int | None = Field(None, description="Total number of pages")
    start_time: str | None = Field(None, description="Start time")


class TaskStatusData(BaseModel):
    """Data field in task status response."""

    task_id: str = Field(..., description="Task ID")
    state: MineRUTaskState = Field(..., description="Current task state")
    data_id: str | None = Field(None, description="Data ID")
    extract_progress: ExtractProgress | None = Field(
        None, description="Extraction progress (when state=running)"
    )
    full_zip_url: str | None = Field(
        None, description="URL to download the result ZIP file"
    )
    err_msg: str | None = Field(None, description="Error message if task failed")


class TaskStatusResponse(BaseModel):
    """Response from querying task status."""

    code: int = Field(..., description="API status code, 0 for success")
    msg: str = Field(..., description="API message")
    trace_id: str | None = Field(None, description="Trace ID for debugging")
    data: TaskStatusData | None = Field(None, description="Task status data")

    @property
    def task_id(self) -> str:
        """Get task_id from data field for backward compatibility."""
        if not self.data:
            raise ValueError(_NO_DATA_FIELD_MSG)
        return self.data.task_id

    @property
    def state(self) -> MineRUTaskState:
        """Get state from data field for backward compatibility."""
        if not self.data:
            raise ValueError(_NO_DATA_FIELD_MSG)
        return self.data.state

    @property
    def full_zip_url(self) -> str | None:
        """Get full_zip_url from data field for backward compatibility."""
        if not self.data:
            return None
        return self.data.full_zip_url

    @property
    def err_msg(self) -> str | None:
        """Get err_msg from data field for backward compatibility."""
        if not self.data:
            return None
        return self.data.err_msg

    @property
    def extract_progress(self) -> int | None:
        """Get extract progress as percentage for backward compatibility."""
        if (
            not self.data
            or not self.data.extract_progress
            or not self.data.extract_progress.total_pages
        ):
            return None
        extracted = self.data.extract_progress.extracted_pages or 0
        total = self.data.extract_progress.total_pages
        return int((extracted / total) * 100) if total > 0 else 0


class ParsedResult(BaseModel):
    """Parsed result from MineRU."""

    task_id: str = Field(..., description="Task ID")
    cache_dir: str = Field(..., description="Local cache directory path")
    markdown_path: str = Field(..., description="Path to extracted Markdown file")
    markdown_content: str = Field(..., description="Content of the Markdown file")
