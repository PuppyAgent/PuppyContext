"""Upload-owned multipart API contract. Lifecycle writes belong to Upload."""
from pydantic import BaseModel, Field
from src.infra.task_status import IngestStatus


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
