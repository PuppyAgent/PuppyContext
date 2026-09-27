"""
Unified Ingest Schemas - Gateway layer unified interface definitions.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    """Data source type."""
    FILE = "file"      # → File Worker (ETL)
    SAAS = "saas"      # → ImportJob worker
    URL = "url"        # → ImportJob worker


class IngestType(str, Enum):
    """Specific import type."""
    # File types (FILE source → File Worker)
    PDF = "pdf"
    IMAGE = "image"
    DOCUMENT = "document"  # docx, xlsx, etc.
    TEXT = "text"          # txt, md, json, code files

    # SaaS types (SAAS source → ImportJob or SynchronizeEngine)
    GITHUB = "github"
    NOTION = "notion"
    GMAIL = "gmail"
    GOOGLE_DRIVE = "google_drive"
    GOOGLE_SHEETS = "google_sheets"
    GOOGLE_DOCS = "google_docs"
    GOOGLE_CALENDAR = "google_calendar"
    AIRTABLE = "airtable"
    LINEAR = "linear"

    # URL types
    WEB_PAGE = "web_page"


class IngestStatus(str, Enum):
    """Unified status - maps underlying ETL/Import statuses."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IngestMode(str, Enum):
    """Processing mode (only applicable for FILE type)."""
    SMART = "smart"           # Smart: text files direct save, PDF/images OCR
    RAW = "raw"               # Raw: all files direct save
    STRUCTURED = "structured"  # Structured: all files go through ETL + rules


# === Request Schemas ===

class IngestSubmitRequest(BaseModel):
    """Unified submit request (JSON body, excluding files)."""
    project_id: str = Field(..., description="Target project ID")
    source_type: SourceType = Field(..., description="Source type")

    # SaaS/URL source (source_type = saas | url)
    url: str | None = Field(None, description="SaaS or Web URL")

    # Optional configuration
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


# === Response Schemas ===

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

    # Result
    content_path: str | None = None
    items_count: int | None = None

    # Error
    error: str | None = None

    # Timestamps
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    # Original filename (FILE type)
    filename: str | None = None

    # Metadata
    metadata: dict[str, Any] = Field(default_factory=dict)


class BatchTaskResponse(BaseModel):
    """Batch query response."""
    tasks: list[IngestTaskResponse]
    total: int


# === Backend-Proxied Multipart Upload Schemas ===
#
# Four-step protocol with all bytes flowing through the FastAPI
# process (rather than browser → S3 directly):
#   1. /upload/init     — initiate S3 multipart, allocate a pending
#                         task, return ``upload_id`` + ``s3_key``.
#   2. /upload/part     — browser PUTs each part to the backend; the
#                         backend forwards to S3 via boto3
#                         ``upload_part`` and returns the ``ETag``.
#   3. /upload/complete — finalize the multipart upload + write the
#                         assembled bytes into Version Engine (see batch variant).
#   4. /upload/abort    — cancel an in-flight upload (idempotent).
#
# Why proxy through the backend instead of presigned URLs to S3?
# Some S3-compatible providers (notably Supabase Storage's S3
# emulation) don't expose ``PutBucketCors`` and the dashboard CORS
# settings only cover the REST API, not the S3 endpoint. This means
# direct browser → S3 PUTs are blocked by browser CORS no matter
# what — there's no setting that would fix it. Proxying through
# our FastAPI process sidesteps the issue entirely (browser →
# Next.js is same-origin; Next.js → FastAPI → S3 is server-to-
# server, neither pays the CORS tax). Trade-off: ~15-30% slower
# than direct-to-S3 due to the extra hop, plus backend bandwidth.
# Acceptable because:
#   - typical files are small-medium (PDF/MD/JSON)
#   - large/folder uploads should use the local sync daemon, which
#     survives tab close, network drops, and reboots — strictly
#     better reliability than any browser flow can offer.

class UploadInitFile(BaseModel):
    """One file's worth of metadata in an init request."""
    filename: str = Field(..., description="Original filename")
    size: int = Field(..., ge=0, description="File size in bytes")
    content_type: str | None = Field(None, description="MIME type, optional")
    parent_path: str | None = Field(
        None,
        description=(
            "hash folder path the file should land in. Empty/None == root. "
            "Stored on the task so the finalize worker knows where to write."
        ),
    )


class UploadInitRequest(BaseModel):
    """Begin a multipart upload for one or more files."""
    project_id: str = Field(..., description="Target project ID")
    files: list[UploadInitFile] = Field(..., min_length=1)
    chunk_size: int | None = Field(
        None,
        description=(
            "Bytes per part. Defaults to 8 MiB. AWS requires >= 5 MiB "
            "for every part except the last. Larger chunks reduce HTTP "
            "overhead at the cost of bigger blast radius on a part retry."
        ),
    )


class UploadInitFileResponse(BaseModel):
    """Server-side state needed to drive one file's upload.

    Note: no ``parts`` array of presigned URLs anymore. The browser
    PUTs each part to ``/upload/part`` (same-origin via the Next.js
    proxy), so there's nothing to sign upfront. Total part count is
    derived from ``chunk_size`` and the file size on the client.
    """
    task_id: str
    upload_job_id: str | None = None
    filename: str
    s3_key: str
    upload_id: str
    chunk_size: int
    total_parts: int = Field(
        ..., ge=0, le=10000,
        description=(
            "Number of parts the client should PUT. Zero means the backend "
            "already staged a zero-byte object and the client should skip PUTs."
        ),
    )


class UploadInitResponse(BaseModel):
    upload_job_id: str | None = None
    files: list[UploadInitFileResponse]


class UploadPartResponse(BaseModel):
    """Result of a single PUT to ``/upload/part``."""
    part_number: int = Field(..., ge=1, le=10000)
    etag: str = Field(..., description="ETag returned by S3 for the UploadPart call")


class UploadCompletePart(BaseModel):
    """Echo of one part's upload result returned by the browser."""
    part_number: int = Field(..., ge=1, le=10000)
    etag: str = Field(..., description="ETag returned by S3 for the PutPart response")


class UploadCompleteRequest(BaseModel):
    task_id: str
    s3_key: str
    upload_id: str
    parts: list[UploadCompletePart] = Field(..., min_length=0)


class UploadCompleteResponse(BaseModel):
    task_id: str
    status: IngestStatus
    path: str | None = Field(None, description="version path the file is being written to")


class UploadCompleteItem(BaseModel):
    """One file's parts in a batch finalize call."""
    task_id: str
    s3_key: str
    upload_id: str
    parts: list[UploadCompletePart] = Field(..., min_length=0)


class UploadCompleteBatchRequest(BaseModel):
    """Finalize multiple uploads as a single project-root product commit.

    The whole point: dropping a folder of N files should record as
    one commit ("uploaded N files at HH:MM"), not N commits. Per-file
    overhead (negotiate + push round-trips, ~1.5–2s of supabase RPC)
    is fixed, so collapsing N pushes into 1 cuts wall-clock from
    N×2s down to ~2s for the whole batch. Same as ``git add a b c
    && git commit`` vs three separate commits.

    Files in a batch may land under paths that also have access-point
    scopes. The product upload still records one root transaction; child
    scope refs are derived afterwards for Git/AP clients.
    """
    items: list[UploadCompleteItem] = Field(..., min_length=1)


class UploadCompleteItemResult(BaseModel):
    """Per-file outcome inside a batch response."""
    task_id: str
    status: IngestStatus
    path: str | None = None
    error: str | None = Field(
        None,
        description=(
            "Failure reason for this item only. Other items in the "
            "batch may still have succeeded — this protocol is "
            "best-effort per file."
        ),
    )


class UploadCompleteBatchResponse(BaseModel):
    """Per-file outcomes for a batch finalize.

    Returned even on partial failure (status code 200) — the client
    must walk ``items`` and surface failures individually rather
    than treating the whole batch as one transaction. We chose
    partial-success-with-200 over all-or-nothing-with-500 because:
      - the user has already paid the bandwidth to upload all parts;
        bouncing the whole batch would force re-upload of the
        successful files
      - the typical failure mode is one weird file in N (mount
        path collision, ETag mismatch), not "everything is broken"
    """
    items: list[UploadCompleteItemResult]


class UploadAbortRequest(BaseModel):
    task_id: str
    s3_key: str
    upload_id: str


class UploadAbortResponse(BaseModel):
    task_id: str
    cancelled: bool
