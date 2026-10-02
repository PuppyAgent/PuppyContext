"""
ETL API Schemas

Pydantic models for ETL API requests and responses.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.platform.upload.tasks.models import ETLTaskStatus


class UploadAndSubmitItem(BaseModel):
    """Per-file result for upload_and_submit."""

    filename: str = Field(..., description="Original filename")
    task_id: str = Field(..., description="ETL task ID (created even if upload failed)")
    status: ETLTaskStatus = Field(..., description="Initial task status")
    s3_key: str | None = Field(
        default=None, description="Uploaded raw S3 key (None if upload failed)"
    )
    error: str | None = Field(default=None, description="Error message if failed")


class UploadAndSubmitResponse(BaseModel):
    """Response for upload_and_submit."""

    items: list[UploadAndSubmitItem] = Field(default_factory=list)
    total: int = Field(..., description="Total items returned")


class ETLTaskResponse(BaseModel):
    """Response for ETL task status query."""

    task_id: str
    created_by: str | None = None
    project_id: str
    filename: str
    rule_id: int
    status: ETLTaskStatus
    progress: int
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ETLTaskListResponse(BaseModel):
    """Response for ETL task list query."""

    tasks: list[ETLTaskResponse]
    total: int
    limit: int
    offset: int


class ETLRuleCreateRequest(BaseModel):
    """Request to create an ETL rule."""

    name: str = Field(..., description="Rule name")
    description: str = Field(..., description="Rule description")
    json_schema: dict[str, Any] | None = Field(
        None, description="JSON Schema for output (required for llm)"
    )
    system_prompt: str | None = Field(None, description="Optional system prompt")
    postprocess_mode: str | None = Field(default=None, description="llm|skip")
    postprocess_strategy: str | None = Field(
        default=None, description="Postprocess strategy (optional)"
    )


class ETLRuleResponse(BaseModel):
    """Response for ETL rule query."""

    rule_id: int
    name: str
    description: str
    json_schema: dict[str, Any]
    system_prompt: str | None
    postprocess_mode: str | None = None
    postprocess_strategy: str | None = None
    created_at: datetime
    updated_at: datetime


class ETLRuleListResponse(BaseModel):
    """Response for ETL rule list query."""

    rules: list[ETLRuleResponse]
    total: int
    limit: int
    offset: int


class ETLHealthResponse(BaseModel):
    """Health check response for ETL service."""

    status: str
    queue_size: int
    task_count: int
    worker_count: int


class BatchETLTaskStatusResponse(BaseModel):
    """Response for batch ETL task status query."""

    tasks: list[ETLTaskResponse] = Field(..., description="List of task statuses")
    total: int = Field(..., description="Total number of tasks queried")


class ETLCancelResponse(BaseModel):
    """Response for task cancellation."""

    task_id: str
    status: ETLTaskStatus
    message: str


class ETLRetryRequest(BaseModel):
    """Request to retry an ETL task from a given stage."""

    from_stage: str = Field(..., description="Retry stage: mineru|postprocess")


class ETLRetryResponse(BaseModel):
    """Response for task retry."""

    task_id: str
    status: ETLTaskStatus
    message: str
