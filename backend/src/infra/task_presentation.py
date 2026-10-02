"""Read-only task presentation vocabulary shared by entrypoint HTTP facades.

These are response envelopes, not persistent jobs/bindings or state machines.
"""
from enum import Enum
from pydantic import BaseModel
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
