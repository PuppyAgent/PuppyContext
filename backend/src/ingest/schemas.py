"""Legacy ingestion facade and read-only aggregated task presentation."""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from src.infra.task_status import IngestStatus


from src.infra.task_presentation import (
    SourceType, IngestType, IngestSubmitItem, IngestSubmitResponse,
)


class IngestMode(str, Enum):
    """Processing mode (only applicable for FILE type)."""
    SMART = "smart"
    RAW = "raw"
    STRUCTURED = "structured"


class IngestSubmitRequest(BaseModel):
    """Unified submit request (JSON body, excluding files)."""
    project_id: str = Field(..., description="Target project ID")
    source_type: SourceType = Field(..., description="Source type")
    url: str | None = Field(None, description="SaaS or Web URL")
    name: str | None = Field(None, description="Custom name")
    mode: IngestMode = Field(IngestMode.SMART, description="Processing mode")
    rule_id: int | None = Field(None, description="ETL rule ID")
    path: str | None = Field(None, description="Target version path")
    crawl_options: dict | None = Field(None, description="URL crawl options")
    sync_config: dict | None = Field(None, description="Sync configuration")


class BatchTaskQuery(BaseModel):
    """Single item for batch query."""
    task_id: str
    source_type: SourceType


class BatchQueryRequest(BaseModel):
    """Batch query request."""
    tasks: list[BatchTaskQuery]


class IngestTaskResponse(BaseModel):
    """Task status response."""
    task_id: str
    source_type: SourceType
    ingest_type: IngestType
    status: IngestStatus
    progress: int = Field(0, ge=0, le=100)
    message: str | None = None
    content_path: str | None = None
    items_count: int | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    filename: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BatchTaskResponse(BaseModel):
    """Batch query response."""
    tasks: list[IngestTaskResponse]
    total: int
