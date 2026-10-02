"""Legacy ingestion facade and read-only aggregated task presentation."""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from src.infra.task_status import IngestStatus


class SourceType(str, Enum):
    """Data source type."""
    FILE = "file"
    SAAS = "saas"
    URL = "url"


class IngestType(str, Enum):
    """Specific import type."""
    PDF = "pdf"
    IMAGE = "image"
    DOCUMENT = "document"
    TEXT = "text"
    GITHUB = "github"
    NOTION = "notion"
    GMAIL = "gmail"
    GOOGLE_DRIVE = "google_drive"
    GOOGLE_SHEETS = "google_sheets"
    GOOGLE_DOCS = "google_docs"
    GOOGLE_CALENDAR = "google_calendar"
    AIRTABLE = "airtable"
    LINEAR = "linear"
    WEB_PAGE = "web_page"


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


class IngestSubmitItem(BaseModel):
    """Single submit result."""
    task_id: str
    source_type: SourceType
    ingest_type: IngestType
    status: IngestStatus
    filename: str | None = None
    s3_key: str | None = None
    path: str | None = None
    error: str | None = None


class IngestSubmitResponse(BaseModel):
    """Submit response."""
    items: list[IngestSubmitItem]
    total: int


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
