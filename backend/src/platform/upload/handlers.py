"""
Ingest Router - Unified entry point API.

This router provides a unified interface for all data ingestion:
- FILE: Local file upload → File Worker (ETL)
- SAAS: SaaS/URL import → durable ImportJob worker

Dual-layer routing architecture:
- Layer 1: mode (raw | ocr_parse)
- Layer 2: file_type (json | text | ocr_needed | binary)
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)

from src.exceptions import AppException
from src.infra.file_formats import detect_mime, detect_node_type
from src.infra.s3.dependencies import get_s3_service
from src.infra.s3.exceptions import S3Error, S3FileSizeExceededError, S3MultipartError
from src.infra.s3.service import S3Service

# Import underlying services for file processing
from src.ingest.file.dependencies import get_etl_service
from src.ingest.file.exceptions import RuleNotFoundError
from src.ingest.file.service import ETLService
from src.ingest.file.tasks.models import ETLTaskStatus
from src.ingest.policy.upload_policy import (
    PER_BATCH_MAX_BYTES as POLICY_PER_BATCH_MAX_BYTES,
)
from src.ingest.policy.upload_policy import (
    PER_FILE_MAX_BYTES as POLICY_PER_FILE_MAX_BYTES,
)
from src.ingest.policy.upload_policy import (
    evaluate_batch_limits,
    path_has_blocked_segment,
)
from src.ingest.schemas import (
    IngestStatus,
    IngestSubmitItem,
    IngestSubmitResponse,
    IngestType,
    SourceType,
    UploadAbortRequest,
    UploadAbortResponse,
    UploadCompleteBatchRequest,
    UploadCompleteBatchResponse,
    UploadCompleteItemResult,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitFileResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadPartResponse,
)
from src.ingest.shared.task.normalizers import detect_file_ingest_type
from src.ingest.upload_jobs import UploadJobRepository
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.billing.runtime import guard_unmetered_hosted_runtime
from src.platform.entitlements.dependencies import get_entitlement_service
from src.platform.entitlements.service import EntitlementService

logger = logging.getLogger(__name__)

router = APIRouter()


def _authorize_owned_upload_task(
    task,
    *,
    current_user: CurrentUser,
    authorization: AuthorizationService,
):
    """Require both task ownership and the current Project write grant.

    Ownership prevents task-id substitution; it is only a child-resource
    restriction.  Re-resolving the Project grant closes the window where a
    member starts a multipart upload and is then revoked or downgraded before
    a part/finalize request.
    """

    if not task or (task.created_by is not None and task.created_by != current_user.user_id):
        raise HTTPException(status_code=404, detail="Task not found")
    authorization.authorize(
        task.project_id,
        current_user.user_id,
        ProjectAction.INGEST_WRITE,
    )
    return task


# === File Type Classification ===

_OCR_DOCUMENT_MIME_PREFIXES = (
    "application/msword",
    "application/vnd.ms-",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
    "application/rtf",
    "text/rtf",
    "application/epub+zip",
    "application/x-mobipocket-ebook",
)


def classify_file_type(filename: str) -> str:
    """Classify an upload using the canonical file-format registry.

    This legacy/simple upload endpoint needs coarse routing buckets
    (json/text/ocr_needed/binary), but the knowledge about extensions
    and filenames must stay in `src.infra.file_formats`. Keeping a
    local extension list here was the old architecture smell.
    """
    node_type = detect_node_type(filename)
    if node_type == "json":
        return "json"

    ingest_type = detect_file_ingest_type(filename)
    if ingest_type == IngestType.TEXT:
        return "text"

    mime = detect_mime(filename).lower()
    if ingest_type in {IngestType.PDF, IngestType.IMAGE} or any(
        mime.startswith(prefix) for prefix in _OCR_DOCUMENT_MIME_PREFIXES
    ):
        return "ocr_needed"

    return "binary"


# === File Upload Endpoint ===


@router.post("/submit/file", response_model=IngestSubmitResponse, status_code=202)
async def submit_file_ingest(
    # Required fields
    project_id: str = Form(..., description="Target project ID"),
    files: list[UploadFile] = File(..., description="Files to upload"),
    # Optional configuration
    # Default is "raw" so callers that don't opt into the OCR
    # pipeline never accidentally trigger MineRU/LLM. Was "ocr_parse"
    # historically, which made the smart-parse pipeline the implicit
    # default and burned worker cycles for clients that just wanted
    # a file dropped into the tree.
    mode: str = Form("raw", description="Processing mode: raw | ocr_parse"),
    rule_id: int | None = Form(None, description="ETL rule ID (for ocr_parse mode)"),
    parent_path: str | None = Form(None, description="Parent directory path for new files"),
    # Legacy alias kept so older frontend callers that still send
    # `parent_id` continue to land files in the intended ObjectStore path.
    # The content tree is path-based, so new callers should use
    # `parent_path`.
    parent_id: str | None = Form(None, description="Deprecated alias for parent_path"),
    # Dependencies
    etl_service: ETLService = Depends(get_etl_service),
    s3_service: S3Service = Depends(get_s3_service),
    entitlement_service: EntitlementService = Depends(get_entitlement_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Submit file ingest tasks.

    All text/JSON files are written directly to the version tree via Write Engine.
    Binary/OCR files go to S3 + ETL Worker (when OCR is enabled).
    When `settings.ENABLE_OCR` is False any incoming `mode="ocr_parse"`
    is downgraded to "raw" so binary/OCR-needing files end up on S3
    via the same code path as a plain file upload.
    """
    grant = authorization.authorize(project_id, current_user.user_id, ProjectAction.INGEST_WRITE)
    entitlement_file_max = entitlement_service.enforced_limit_value(
        grant.org_id,
        "upload.max_single_file_bytes",
    )
    per_file_max_bytes = (
        int(entitlement_file_max) if entitlement_file_max is not None else POLICY_PER_FILE_MAX_BYTES
    )

    # OCR pause: silently downgrade ocr_parse → raw when the
    # smart-parse pipeline is disabled at the deployment level.
    # We log once per request rather than per-file because callers
    # batch dozens of files into one POST and we don't want to spam
    # the log. Using the module-level `settings` singleton (rather
    # than constructing `Settings()` per request) keeps env-file
    # parsing off the hot path.
    from src.config import settings as app_settings

    if mode == "ocr_parse" and not app_settings.ENABLE_OCR:
        logger.warning(
            "OCR pipeline is paused (ENABLE_OCR=False); downgrading "
            "ocr_parse → raw for project=%s, %d file(s).",
            project_id,
            len(files),
        )
        mode = "raw"
    if mode == "ocr_parse":
        guard_unmetered_hosted_runtime("ingest.ocr_parse")

    from src.platform.project.write_lease import build_leased_worker_write_commands

    commands = build_leased_worker_write_commands()

    target_parent_path = (parent_path or parent_id or "").strip("/")

    items: list[IngestSubmitItem] = []
    modified_files: dict[str, bytes] = {}

    for f in files:
        original_filename = f.filename or "file"
        original_basename = Path(original_filename).name
        content = await f.read()
        if len(content) > per_file_max_bytes:
            error = (
                f"File '{original_filename}' is {len(content)} bytes; "
                f"per-file cap is {per_file_max_bytes} bytes."
            )
            items.append(
                _make_failed_item(
                    etl_service.create_failed_task(
                        user_id=current_user.user_id,
                        project_id=project_id,
                        filename=original_filename,
                        rule_id=rule_id,
                        error=error,
                        metadata={"error_stage": "entitlement"},
                    ),
                    original_filename,
                    None,
                    error,
                )
            )
            continue

        file_type = classify_file_type(original_basename)

        file_path = (
            f"{target_parent_path}/{original_basename}" if target_parent_path else original_basename
        )

        try:
            if file_type == "json":
                try:
                    text_content = content.decode("utf-8", errors="ignore")
                    json_data = json.loads(text_content)
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON parse failed for {original_filename}: {e}")
                    json_data = {
                        "_raw": content.decode("utf-8", errors="ignore"),
                        "_parse_error": str(e),
                    }

                json_bytes = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
                modified_files[file_path] = json_bytes

                task = _create_completed_task(
                    etl_service,
                    current_user.user_id,
                    project_id,
                    original_filename,
                    rule_id,
                    file_path,
                    "json",
                )
                items.append(_make_completed_item(task, original_filename, file_path))

            elif file_type == "text":
                modified_files[file_path] = content

                task = _create_completed_task(
                    etl_service,
                    current_user.user_id,
                    project_id,
                    original_filename,
                    rule_id,
                    file_path,
                    "markdown",
                )
                items.append(_make_completed_item(task, original_filename, file_path))

            elif file_type == "ocr_needed" and mode == "ocr_parse":
                s3_key = await _upload_to_s3(
                    s3_service, project_id, original_filename, content, f.content_type
                )

                try:
                    task = await etl_service.submit_etl_task(
                        user_id=current_user.user_id,
                        project_id=project_id,
                        filename=original_filename,
                        rule_id=rule_id,
                        s3_key=s3_key,
                    )
                except RuleNotFoundError as e:
                    items.append(
                        _make_failed_item(
                            etl_service.create_failed_task(
                                user_id=current_user.user_id,
                                project_id=project_id,
                                filename=original_filename,
                                rule_id=rule_id,
                                error=str(e),
                            ),
                            original_filename,
                            s3_key,
                            str(e),
                        )
                    )
                    continue

                task.metadata["mount_path"] = file_path
                task.metadata["s3_key"] = s3_key
                etl_service.task_repository.update_task(task)

                items.append(
                    IngestSubmitItem(
                        task_id=str(task.task_id or 0),
                        source_type=SourceType.FILE,
                        ingest_type=detect_file_ingest_type(original_filename),
                        status=IngestStatus.PENDING
                        if task.status == ETLTaskStatus.PENDING
                        else IngestStatus.PROCESSING,
                        filename=original_filename,
                        s3_key=s3_key,
                        path=file_path,
                    )
                )

            else:
                # Generic binary path — hit by:
                #   (a) file_type == "binary" (zip, exe, mp4, …)
                #   (b) file_type == "ocr_needed" with mode=="raw"
                #       (PDF / image / docx after the OCR pause
                #       downgrade above; previously they went through
                #       the ETL worker, which would eventually write
                #       to ObjectStore itself).
                #
                # Without writing into `modified_files` here, the file
                # gets stashed in S3 + a "completed" task row but never
                # appears in the explorer — the bug a user just hit:
                # uploaded PDFs while OCR was paused, the task panel
                # said "Completed", but the file never showed up in the
                # tree. Keep the S3 upload too so any downstream code
                # that consumes `s3_key` keeps working; the dual write
                # is redundant on storage but trivial to revert once
                # we either retire S3 for raw uploads or bring OCR
                # back online.
                s3_key = await _upload_to_s3(
                    s3_service, project_id, original_filename, content, f.content_type
                )

                modified_files[file_path] = content

                task = _create_completed_task(
                    etl_service,
                    current_user.user_id,
                    project_id,
                    original_filename,
                    rule_id,
                    file_path,
                    "file",
                )
                items.append(_make_completed_item(task, original_filename, file_path, s3_key))

        except S3FileSizeExceededError as e:
            items.append(
                _make_failed_item(
                    etl_service.create_failed_task(
                        user_id=current_user.user_id,
                        project_id=project_id,
                        filename=original_filename,
                        rule_id=rule_id,
                        error=str(e),
                        metadata={"error_stage": "upload"},
                    ),
                    original_filename,
                    None,
                    str(e),
                )
            )
        except (S3Error, Exception) as e:
            logger.error(f"File ingest failed for {original_filename}: {e}", exc_info=True)
            items.append(
                _make_failed_item(
                    etl_service.create_failed_task(
                        user_id=current_user.user_id,
                        project_id=project_id,
                        filename=original_filename,
                        rule_id=rule_id,
                        error=f"Import failed: {e}",
                        metadata={"error_stage": "process"},
                    ),
                    original_filename,
                    None,
                    str(e),
                )
            )

    if modified_files:
        try:
            await commands.bulk_write(
                project_id,
                modified_files,
                actor=f"upload:{current_user.user_id}",
                message=f"Upload {len(modified_files)} file(s)",
                source_channel="upload",
            )
        except Exception as e:
            logger.error(f"version push failed during file ingest: {e}", exc_info=True)

    return IngestSubmitResponse(items=items, total=len(items))


# === Helper Functions ===


async def _upload_to_s3(
    s3_service: S3Service,
    project_id: str,
    original_filename: str,
    content: bytes,
    content_type: str | None,
) -> str:
    _, ext = os.path.splitext(original_filename)
    safe_filename = f"{uuid.uuid4()}{ext}"
    s3_key = f"projects/{project_id}/files/{safe_filename}"

    original_filename_b64 = base64.b64encode(original_filename.encode("utf-8")).decode("ascii")

    await s3_service.upload_file(
        key=s3_key,
        content=content,
        content_type=content_type,
        metadata={
            "original_filename_b64": original_filename_b64,
            "project_id": str(project_id),
        },
    )
    return s3_key


def _create_completed_task(
    etl_service: ETLService,
    user_id: str,
    project_id: str,
    filename: str,
    rule_id: int | None,
    path: str,
    node_type: str,
):
    task = etl_service.create_failed_task(
        user_id=user_id,
        project_id=project_id,
        filename=filename,
        rule_id=rule_id,
        error="Direct import completed",
        metadata={
            "direct_import": True,
            "path": path,
            "node_type": node_type,
        },
    )
    task.status = ETLTaskStatus.COMPLETED
    task.error = None
    task.progress = 100
    etl_service.task_repository.update_task(task)
    return task


def _make_completed_item(
    task,
    filename: str,
    path: str,
    s3_key: str | None = None,
) -> IngestSubmitItem:
    return IngestSubmitItem(
        task_id=str(task.task_id or 0),
        source_type=SourceType.FILE,
        ingest_type=detect_file_ingest_type(filename),
        status=IngestStatus.COMPLETED,
        filename=filename,
        s3_key=s3_key,
        path=path,
    )


def _make_failed_item(
    task,
    filename: str,
    s3_key: str | None,
    error: str,
) -> IngestSubmitItem:
    return IngestSubmitItem(
        task_id=str(task.task_id or 0),
        source_type=SourceType.FILE,
        ingest_type=detect_file_ingest_type(filename),
        status=IngestStatus.FAILED,
        filename=filename,
        s3_key=s3_key,
        error=error,
    )


# === Backend-Proxied Multipart Upload Endpoints ===
#
# Four-step protocol with all bytes flowing through the FastAPI
# process (browser → Next.js same-origin → FastAPI → S3):
#   1. POST /upload/init     — initiate S3 multipart upload, create
#                              the pending task, return ``upload_id``
#                              + ``s3_key`` + ``total_parts``.
#   2. PUT  /upload/part     — browser PUTs each part to FastAPI;
#                              FastAPI calls boto3 ``upload_part`` to
#                              hand bytes to S3 and returns the
#                              ``ETag`` to the client.
#   3. POST /upload/complete — finalize the multipart upload + write
#                              the assembled bytes into ObjectStore (see
#                              batch variant for folder uploads).
#   4. POST /upload/abort    — (cancel path) drop in-flight upload.
#
# Why proxy instead of presigned-URL direct-to-S3?
#   Some S3-compatible providers (notably Supabase Storage's S3
#   emulation) don't expose ``PutBucketCors`` and the dashboard CORS
#   settings only cover their REST API, not the S3 endpoint. Direct
#   browser → S3 PUTs from any web origin are blocked by browser
#   CORS no matter what — there's no setting that fixes it. Proxying
#   through FastAPI sidesteps the issue entirely (browser → Next.js
#   is same-origin; Next.js → FastAPI → S3 is server-to-server,
#   neither pays the CORS tax).
#
#   Trade-off: ~15-30% slower than direct-to-S3 due to the extra
#   hop, plus backend bandwidth (each part flows through us once).
#   Acceptable because typical files are small-medium and the
#   simplicity win (one upload path that works on every backend) is
#   worth way more than the 30% speed delta. For genuinely large /
#   reliability-sensitive workloads, users should use the local
#   sync daemon, which survives tab close, network drops, and
#   reboots — strictly better reliability than any web upload can
#   ever offer.

# AWS hard limits + sensible defaults. ``MAX_FILE_SIZE`` is a safety
# rail; the bucket itself can hold up to 5TiB per object.
_DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
_MIN_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MiB (AWS minimum, except last part)
_MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024  # 5 GiB
_MAX_PARTS_PER_UPLOAD = 10000  # AWS hard limit
# Per-part request body cap (8 MiB chunk + slack for the last-part
# overshoot some clients send). Used to short-circuit oversized
# bodies before we forward them to S3 and pay for the bandwidth.
_MAX_PART_BODY_SIZE = 32 * 1024 * 1024  # 32 MiB


def _metadata_int(metadata: dict, key: str) -> int | None:
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _expected_part_size(metadata: dict, part_number: int) -> int:
    declared_size = _metadata_int(metadata, "size")
    chunk_size = _metadata_int(metadata, "chunk_size")
    total_parts = _metadata_int(metadata, "total_parts")
    if declared_size is None or chunk_size is None or total_parts is None:
        raise HTTPException(
            status_code=400,
            detail="Task is missing upload shape metadata; restart the upload",
        )
    if total_parts <= 0:
        raise HTTPException(status_code=400, detail="Task has no multipart parts")
    if part_number > total_parts:
        raise HTTPException(
            status_code=400,
            detail=f"part_number {part_number} exceeds total_parts {total_parts}",
        )
    expected_start = (part_number - 1) * chunk_size
    expected_size = min(chunk_size, declared_size - expected_start)
    if expected_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid part number for task size")
    return expected_size


def _validate_part_body_shape(metadata: dict, part_number: int, body_size: int) -> None:
    expected_size = _expected_part_size(metadata, part_number)
    if body_size != expected_size:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Part {part_number} is {body_size} bytes; expected "
                f"{expected_size} bytes for this upload"
            ),
        )


def _validate_complete_manifest(metadata: dict, parts: list) -> None:
    declared_size = _metadata_int(metadata, "size")
    total_parts = _metadata_int(metadata, "total_parts")
    if declared_size is None or total_parts is None:
        raise HTTPException(
            status_code=400,
            detail="Task is missing upload shape metadata; restart the upload",
        )
    part_numbers = [p.part_number for p in parts]
    if declared_size == 0:
        if part_numbers:
            raise HTTPException(status_code=400, detail="Zero-byte upload must not include parts")
        return
    expected_numbers = list(range(1, total_parts + 1))
    sorted_numbers = sorted(part_numbers)
    if sorted_numbers != expected_numbers:
        raise HTTPException(
            status_code=400,
            detail=(
                "Upload parts do not match the server-issued manifest; "
                f"expected {expected_numbers}, got {sorted_numbers}"
            ),
        )


@router.post("/upload/init", response_model=UploadInitResponse)
async def init_multipart_upload(
    request: UploadInitRequest,
    s3_service: S3Service = Depends(get_s3_service),
    etl_service: ETLService = Depends(get_etl_service),
    entitlement_service: EntitlementService = Depends(get_entitlement_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Begin a backend-proxied multipart upload for one or more files.

    For each file we:
      1. Verify project access (the auth boundary; the subsequent
         ``/upload/part`` and ``/upload/complete`` calls re-verify
         task ownership against the user-bound task we create here).
      2. Allocate a unique S3 key namespaced by project + user.
      3. Initiate a multipart upload on S3 — gets us an
         ``UploadId`` we'll thread through subsequent calls.
      4. Create a ``pending`` task in the ``uploads`` table so client
         polling has a real ID from the very first frame.

    Crucially we do NOT pre-sign per-part URLs anymore. Parts are
    PUT to the proxied ``/upload/part`` endpoint, which performs
    ``upload_part`` server-side. See the protocol comment above for
    why we abandoned direct-to-S3.
    """
    grant = authorization.authorize(
        request.project_id, current_user.user_id, ProjectAction.INGEST_WRITE
    )

    chunk_size = request.chunk_size or _DEFAULT_CHUNK_SIZE
    if chunk_size < _MIN_CHUNK_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_size must be >= {_MIN_CHUNK_SIZE} bytes (AWS minimum)",
        )
    if chunk_size > _MAX_PART_BODY_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_size must be <= {_MAX_PART_BODY_SIZE} bytes",
        )

    entitlement_file_max = entitlement_service.enforced_limit_value(
        grant.org_id,
        "upload.max_single_file_bytes",
    )
    per_file_max_bytes = (
        int(entitlement_file_max) if entitlement_file_max is not None else POLICY_PER_FILE_MAX_BYTES
    )
    entitlement_batch_max = entitlement_service.enforced_limit_value(
        grant.org_id,
        "upload.max_batch_bytes",
    )
    batch_max_bytes = (
        int(entitlement_batch_max)
        if entitlement_batch_max is not None
        else POLICY_PER_BATCH_MAX_BYTES
    )

    # ── PUP-3 batch caps ───────────────────────────────────────────
    # Defense-in-depth (Q8): the client also enforces these but a
    # third-party tool / future SDK / curl request could bypass the
    # client. The numbers come from the policy module
    # (``src.ingest.policy.upload_policy``); see
    # ``docs/proposals/PUP-3-folder-upload-policy.md`` Q4.
    for violation in evaluate_batch_limits(
        (f.size for f in request.files),
        max_total_bytes=batch_max_bytes,
    ):
        if violation.kind == "file_count":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Batch exceeds {violation.limit} files "
                    f"(requested {violation.actual}). Split into smaller "
                    f"uploads or use the CLI for bulk ingestion."
                ),
            )
        if violation.kind == "total_bytes":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Batch total {violation.actual} bytes exceeds "
                    f"{violation.limit} bytes. Split into smaller uploads."
                ),
            )

    # Validate every file FIRST, synchronously. Range / part-count
    # errors are deterministic; we want the request to fail with
    # 400 before we go and create any S3 multiparts that we'd then
    # have to clean up. This also keeps the parallel path below
    # exception-free for client errors — only S3/Supabase failures
    # can reach gather.
    prepared_files: list[dict] = []
    for f in request.files:
        # The per-file policy cap (100 MB, PUP-3) is the stricter of
        # the two; ``_MAX_FILE_SIZE`` (5 GiB) is the legacy multipart
        # ceiling kept for the chunk-count check below. Enforce the
        # policy cap first so users see the policy-level message,
        # not the larger S3-multipart message.
        if f.size > per_file_max_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File '{f.filename}' is {f.size} bytes; per-file "
                    f"cap is {per_file_max_bytes} bytes."
                ),
            )
        if f.size > _MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File '{f.filename}' is {f.size} bytes; max allowed is {_MAX_FILE_SIZE} bytes"
                ),
            )

        num_parts = 0 if f.size == 0 else max(1, (f.size + chunk_size - 1) // chunk_size)
        if num_parts > _MAX_PARTS_PER_UPLOAD:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File '{f.filename}' would require {num_parts} parts at "
                    f"chunk_size={chunk_size}; AWS caps multipart at "
                    f"{_MAX_PARTS_PER_UPLOAD}. Increase chunk_size."
                ),
            )

        original_basename = Path(f.filename).name
        _, ext = os.path.splitext(original_basename)
        # Key layout: ``projects/{pid}/uploads/{uid}/{uuid}{ext}``.
        # Project + user prefix lets us scope auth on subsequent
        # ``/upload/part`` calls (the task echoes the s3_key back to
        # the client; we cross-check the echo).
        s3_key = f"projects/{request.project_id}/uploads/{current_user.user_id}/{uuid.uuid4()}{ext}"

        parent_path = (f.parent_path or "").strip("/")
        mount_path = f"{parent_path}/{original_basename}" if parent_path else original_basename

        # PUP-3 hardcoded blocklist (Q1, Q8): reject any segment of
        # the resulting mount_path that matches a blocked name (``.git``,
        # ``node_modules``, etc.). Clients are supposed to filter these
        # before they ever hit /upload/init; this is the defense-in-
        # depth wall that closes the regression / third-party-client
        # bypass risk.
        is_blocked, blocked_seg = path_has_blocked_segment(mount_path)
        if is_blocked:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "policy_blocked",
                    "segment": blocked_seg,
                    "path": mount_path,
                    "message": (
                        f"Path '{mount_path}' contains the blocked segment "
                        f"'{blocked_seg}' (PUP-3 folder-upload policy). "
                        f"Remove that folder from the upload, or use the Git "
                        f"protocol if you need repository history."
                    ),
                },
            )

        # Tag S3 metadata so back-fill / debug tooling can recover the
        # original filename (which may contain non-ASCII chars S3
        # rejects in raw user-metadata values).
        original_filename_b64 = base64.b64encode(f.filename.encode("utf-8")).decode("ascii")

        prepared_files.append(
            {
                "f": f,
                "num_parts": num_parts,
                "s3_key": s3_key,
                "mount_path": mount_path,
                "original_filename_b64": original_filename_b64,
            }
        )

    # Resolve default rule_id ONCE. The legacy ``uploads`` schema
    # requires a non-null rule_id on every row but the finalize
    # worker doesn't invoke the rule engine — so the value is
    # purely a placeholder. Resolving it once and reusing across
    # all per-file inserts saves (N-1) × ~400ms in etl_rules
    # queries alone for an N-file batch. The lookup itself is sync
    # Supabase work so we offload to a thread.
    default_rule_id = await asyncio.to_thread(
        etl_service.get_default_rule_id_for_user,
        current_user.user_id,
    )

    upload_job_repo = UploadJobRepository()
    upload_job_id = await asyncio.to_thread(
        upload_job_repo.create_job,
        project_id=request.project_id,
        created_by=current_user.user_id,
        target_path="",
        mode="raw",
        config={
            "file_count": len(prepared_files),
            "chunk_size": chunk_size,
        },
        policy_summary={
            "per_file_max_bytes": per_file_max_bytes,
            "batch_max_bytes": batch_max_bytes,
            "entitlements_enabled": entitlement_service.enabled,
        },
    )

    # Per-file init: create_multipart_upload (S3) + create_pending_upload_task
    # (Supabase). Each is independent of the others, so we fan
    # them out under a semaphore. Sequential previously cost
    # ~1.2s × N files; parallel collapses that to one round-trip
    # cycle for the batch (capped at 8 in flight).
    _INIT_PARALLEL_LIMIT = 8
    sem = asyncio.Semaphore(_INIT_PARALLEL_LIMIT)

    async def _init_one_file(prepared: dict) -> UploadInitFileResponse:
        f = prepared["f"]
        s3_key = prepared["s3_key"]
        mount_path = prepared["mount_path"]
        original_filename_b64 = prepared["original_filename_b64"]
        num_parts = prepared["num_parts"]

        async with sem:
            s3_metadata = {
                "project_id": str(request.project_id),
                "user_id": str(current_user.user_id),
                "original_filename_b64": original_filename_b64,
            }
            if f.size == 0:
                await s3_service.upload_file(
                    key=s3_key,
                    content=b"",
                    content_type=f.content_type,
                    metadata=s3_metadata,
                )
                upload_id = ""
            else:
                upload_id = await s3_service.create_multipart_upload(
                    key=s3_key,
                    content_type=f.content_type,
                    metadata=s3_metadata,
                )

            try:
                # ``create_pending_upload_task`` is sync (Supabase
                # REST), so wrap in to_thread to actually
                # parallelize. With ``default_rule_id`` pre-resolved,
                # this collapses to a single uploads INSERT per task.
                task = await asyncio.to_thread(
                    etl_service.create_pending_upload_task,
                    user_id=current_user.user_id,
                    project_id=request.project_id,
                    filename=f.filename,
                    s3_key=s3_key,
                    upload_id=upload_id,
                    mount_path=mount_path,
                    size=f.size,
                    content_type=f.content_type,
                    chunk_size=chunk_size,
                    total_parts=num_parts,
                    max_single_file_bytes=per_file_max_bytes,
                    default_rule_id=default_rule_id,
                    upload_job_id=upload_job_id,
                )
                await asyncio.to_thread(
                    upload_job_repo.create_item,
                    upload_job_id=upload_job_id,
                    item_id=str(task.task_id),
                    relative_path=mount_path,
                    original_name=f.filename,
                    size_bytes=f.size,
                    mime_type=f.content_type,
                    s3_key=s3_key,
                )
            except Exception as e:
                # If task creation fails after we've already
                # initiated the multipart upload, abort it so we
                # don't leak an orphan eating bucket space (AWS
                # keeps multiparts forever unless you have a
                # lifecycle rule). Zero-byte files use a direct
                # PutObject and have no multipart session to abort.
                logger.error(
                    f"Failed to create pending upload task for {f.filename}: {e}",
                    exc_info=True,
                )
                try:
                    if upload_id:
                        await s3_service.abort_multipart_upload(s3_key, upload_id)
                except Exception as abort_err:
                    logger.warning(
                        f"Failed to abort orphaned multipart upload {s3_key}: {abort_err}"
                    )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create upload task: {e}",
                )

            return UploadInitFileResponse(
                task_id=str(task.task_id),
                upload_job_id=upload_job_id,
                filename=f.filename,
                s3_key=s3_key,
                upload_id=upload_id,
                chunk_size=chunk_size,
                total_parts=num_parts,
            )

    # Use ``return_exceptions=True`` to let every per-file coro
    # finish (including its own multipart-abort cleanup) before
    # we surface the first error. With the default
    # ``return_exceptions=False`` semantics, gather cancels
    # in-flight siblings on the first failure — and a coro
    # cancelled mid ``abort_multipart_upload`` would leave the
    # worst kind of orphan: half-aborted with no record.
    raw_results = await asyncio.gather(
        *(_init_one_file(p) for p in prepared_files),
        return_exceptions=True,
    )

    # If any per-file init raised, roll back all successful siblings
    # before surfacing the first error. Because the client never
    # receives the partial success response on a 500, leaving those
    # pending tasks/multipart sessions around would create invisible
    # orphans.
    first_error: HTTPException | BaseException | None = None
    for r in raw_results:
        if isinstance(r, HTTPException):
            first_error = first_error or r
        elif isinstance(r, BaseException):
            first_error = first_error or r

    if first_error is not None:
        try:
            await asyncio.to_thread(
                upload_job_repo.mark_job_failed,
                upload_job_id,
                f"upload/init failed: {first_error}",
            )
        except Exception:
            pass

        async def _cleanup_successful_init(r) -> None:
            if not isinstance(r, UploadInitFileResponse):
                return
            try:
                if r.upload_id:
                    await s3_service.abort_multipart_upload(r.s3_key, r.upload_id)
            except Exception as abort_err:
                logger.warning(
                    f"Failed to abort multipart upload after init "
                    f"batch failure {r.s3_key}: {abort_err}"
                )
            try:
                await asyncio.to_thread(
                    etl_service.task_repository.delete_task,
                    r.task_id,
                )
            except Exception as delete_err:
                logger.warning(
                    f"Failed to delete upload task after init "
                    f"batch failure {r.task_id}: {delete_err}"
                )

        await asyncio.gather(
            *(_cleanup_successful_init(r) for r in raw_results),
            return_exceptions=True,
        )
        if isinstance(first_error, HTTPException):
            raise first_error
        raise HTTPException(
            status_code=500,
            detail=f"upload/init failed: {first_error}",
        )

    return UploadInitResponse(upload_job_id=upload_job_id, files=raw_results)


@router.put("/upload/part", response_model=UploadPartResponse)
async def upload_part(
    request: Request,
    task_id: str = Query(..., description="Task ID issued by /upload/init"),
    part_number: int = Query(..., ge=1, le=10000, description="1-based part index"),
    s3_service: S3Service = Depends(get_s3_service),
    etl_service: ETLService = Depends(get_etl_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    """
    Forward one multipart part to S3 on behalf of the browser.

    Auth boundary:
      - The task is looked up by ``task_id``, must belong to the current user,
        and its current ProjectGrant must still allow ingest writes. The
        ``s3_key`` and ``upload_id`` are pulled
        from the task's metadata, never from the client — so a
        compromised browser can't redirect a part to a different
        upload session by tampering with query parameters.

    Memory model:
      - We read the request body fully into a ``bytes`` buffer
        before calling ``upload_part``. boto3's ``upload_part``
        wants a seekable Body for the SHA-256 signing pass. With
        the default 8 MiB chunk_size and 4 concurrent parts per
        file, peak per-file RAM is ~32 MiB — trivial. For
        pathological 1024-part uploads (5 GiB at 5 MiB chunks) the
        per-file ceiling is the same (still 4 concurrent parts).
      - We DON'T stream the body straight to boto3 to keep this
        endpoint simple; if memory pressure ever shows up in
        profiling we can swap to ``s3.upload_part_copy`` or rewrite
        with ``aiobotocore`` streaming.

    Body size cap:
      - ``Content-Length`` (or actual body size) over
        ``_MAX_PART_BODY_SIZE`` is rejected. Prevents a client from
        OOM'ing the FastAPI worker by sending an obscene part.

    Errors:
      - 404: task not found / not owned.
      - 409: task is no longer pending (already completed/aborted).
      - 400: part_number > total_parts for the task, or body size
             exceeds the cap.
      - 502: S3 ``upload_part`` failed (typically transient — the
             client's retry loop should clear it).
    """
    task = _authorize_owned_upload_task(
        etl_service.task_repository.get_task(task_id),
        current_user=current_user,
        authorization=authorization,
    )

    if task.status != ETLTaskStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Task is not pending (status={task.status.value})",
        )

    s3_key = task.metadata.get("s3_key")
    upload_id = task.metadata.get("upload_id")
    if not s3_key or not upload_id:
        # Defensive: a task with no s3_key/upload_id can't be a
        # multipart upload target. Treat as 404 — the client must
        # have a stale task_id from another flow.
        raise HTTPException(
            status_code=404,
            detail="Task is not a multipart upload",
        )
    expected_part_size = _expected_part_size(task.metadata, part_number)

    # Check Content-Length first for an early bounce; it's cheap.
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            cl = int(content_length_header)
        except ValueError:
            cl = -1
        if cl > _MAX_PART_BODY_SIZE:
            raise HTTPException(
                status_code=400,
                detail=(f"Part body is {cl} bytes; max per part is {_MAX_PART_BODY_SIZE} bytes"),
            )
        if cl >= 0 and cl != expected_part_size:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Part {part_number} is declared as {cl} bytes; expected "
                    f"{expected_part_size} bytes for this upload"
                ),
            )

    body = await request.body()
    if len(body) == 0:
        raise HTTPException(status_code=400, detail="Empty part body")
    if len(body) > _MAX_PART_BODY_SIZE:
        raise HTTPException(
            status_code=400,
            detail=(f"Part body is {len(body)} bytes; max per part is {_MAX_PART_BODY_SIZE} bytes"),
        )
    _validate_part_body_shape(task.metadata, part_number, len(body))

    try:
        etag = await s3_service.upload_part(
            key=s3_key,
            upload_id=upload_id,
            part_number=part_number,
            data=body,
        )
    except S3MultipartError as e:
        # ``upload_part`` failures are most often transient (S3 5xx,
        # connection reset, ETag mismatch on a partial replay). We
        # surface a 502 — same wire signature the client's retry
        # loop already handles, no special-casing needed.
        logger.warning(f"upload_part failed for task {task_id} part {part_number}: {e}")
        raise HTTPException(status_code=502, detail=f"S3 upload_part failed: {e}")

    return UploadPartResponse(part_number=part_number, etag=etag)


@router.post("/upload/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    request: UploadCompleteRequest,
    s3_service: S3Service = Depends(get_s3_service),
    etl_service: ETLService = Depends(get_etl_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    """
    Finalize a direct-to-S3 multipart upload and write the assembled
    bytes into ObjectStore *inline*.

    By the time the client calls this, every part is already in S3.
    We:
      1. Verify task ownership and re-resolve the current Project write grant
         (the auth boundary on the back end of the protocol — the ``s3_key``
         & ``upload_id`` are echoed back
         from the client and we cross-check them against the task we
         created in /upload/init).
      2. Run S3 ``CompleteMultipartUpload`` to assemble the parts.
      3. HEAD the resulting object as a sanity check (size matches
         what the client claimed; non-fatal mismatch is logged).
      4. Pull bytes S3 -> backend RAM, write them into ObjectStore via
         ``finalize_upload_to_version``, mark the task COMPLETED. We do
         this inline (instead of dispatching to the ARQ worker) so
         that:
           - the user sees "Completed" the instant /upload/complete
             returns; no waiting on a separate worker poll cycle.
           - the dev environment doesn't need a separate worker
             process running for normal-sized uploads.
         For pathologically large files (multi-GB) the inline path
         can exceed an HTTP timeout — that's a future enhancement
         (a request-flag that swaps to the ``etl_finalize_upload_job``
         worker path, which is still wired up).
    """
    task = _authorize_owned_upload_task(
        etl_service.task_repository.get_task(request.task_id),
        current_user=current_user,
        authorization=authorization,
    )

    # State guard: complete is a one-way trip from PENDING. Re-calling
    # would race with the finalize worker; the client should consult
    # the task status instead.
    if task.status != ETLTaskStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Task is not pending (status={task.status.value})",
        )

    expected_key = task.metadata.get("s3_key")
    expected_upload_id = task.metadata.get("upload_id")
    if expected_key != request.s3_key or expected_upload_id != request.upload_id:
        # Mismatch usually means a confused/replayed client. Refuse
        # rather than complete a multipart upload bound to a different
        # task — would leave the original task PENDING forever.
        raise HTTPException(
            status_code=400,
            detail="s3_key/upload_id mismatch with the task",
        )

    parts = sorted([(p.part_number, p.etag) for p in request.parts], key=lambda x: x[0])
    _validate_complete_manifest(task.metadata, request.parts)
    expected_size = _metadata_int(task.metadata, "size")

    if expected_size == 0 and not parts:
        logger.info(
            "Task %s is zero-byte; skipping multipart complete",
            request.task_id,
        )
    else:
        try:
            await s3_service.complete_multipart_upload(request.s3_key, request.upload_id, parts)
        except Exception as e:
            logger.error(
                f"complete_multipart_upload failed for task {request.task_id}: {e}",
                exc_info=True,
            )
            # Best-effort cleanup so we don't leave a half-assembled
            # multipart upload on the bucket.
            try:
                if request.upload_id:
                    await s3_service.abort_multipart_upload(request.s3_key, request.upload_id)
            except Exception:
                pass
            task.mark_failed(f"Failed to finalize multipart upload: {e}")
            task.metadata["error_stage"] = "complete_multipart"
            etl_service.task_repository.update_task(task)
            upload_job_id = task.metadata.get("upload_job_id")
            if upload_job_id:
                upload_repo = UploadJobRepository()
                await asyncio.to_thread(
                    upload_repo.mark_item_failed,
                    str(task.task_id),
                    f"S3 complete_multipart_upload failed: {e}",
                )
                await asyncio.to_thread(
                    upload_repo.refresh_job_from_items,
                    upload_job_id,
                )
            raise HTTPException(status_code=502, detail=f"S3 complete_multipart_upload failed: {e}")

    # Sanity-check that S3 sees the object at the expected size.
    # Mismatch is non-fatal but worth logging — could indicate a
    # client that lied about the file size or a part ETag mismatch.
    try:
        meta = await s3_service.get_file_metadata(request.s3_key)
        if expected_size is not None and meta.size != expected_size:
            message = f"Declared size {expected_size} differs from S3 size {meta.size}"
            logger.warning(f"Task {request.task_id}: {message}")
            task.mark_failed(message)
            task.metadata["error_stage"] = "size_mismatch"
            etl_service.task_repository.update_task(task)
            upload_job_id = task.metadata.get("upload_job_id")
            if upload_job_id:
                upload_repo = UploadJobRepository()
                await asyncio.to_thread(
                    upload_repo.mark_item_failed,
                    str(task.task_id),
                    message,
                )
                await asyncio.to_thread(
                    upload_repo.refresh_job_from_items,
                    upload_job_id,
                )
            raise HTTPException(status_code=400, detail=message)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        logger.warning(f"Task {request.task_id}: head_object after complete failed: {e}")

    # Run finalize INLINE: download from S3, write into ObjectStore, mark
    # task COMPLETED. The helper is also the body of the ARQ worker
    # job, so behaviour and runtime-state transitions are identical.
    from src.ingest.file.jobs.jobs import finalize_upload_to_version

    try:
        result = await finalize_upload_to_version(
            task_id=task.task_id,
            repo=etl_service.task_repository,
            s3=s3_service,
            state_repo=etl_service.state_repo,
        )
    except asyncio.CancelledError:
        # Finalize already wrote the FAILED state for us; surface
        # a 504 to the client so they can decide whether to retry.
        upload_job_id = task.metadata.get("upload_job_id")
        if upload_job_id:
            upload_repo = UploadJobRepository()
            await asyncio.to_thread(
                upload_repo.mark_item_failed,
                str(task.task_id),
                "Finalize timed out writing to ObjectStore",
            )
            await asyncio.to_thread(upload_repo.refresh_job_from_items, upload_job_id)
        raise HTTPException(status_code=504, detail="Finalize timed out writing to ObjectStore")

    if not result.get("ok"):
        # Helper has already marked the task FAILED + persisted state.
        upload_job_id = task.metadata.get("upload_job_id")
        if upload_job_id:
            upload_repo = UploadJobRepository()
            await asyncio.to_thread(
                upload_repo.mark_item_failed,
                str(task.task_id),
                str(result.get("error") or "Finalize failed"),
            )
            await asyncio.to_thread(upload_repo.refresh_job_from_items, upload_job_id)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to finalize upload: {result.get('error', 'unknown error')}",
        )

    upload_job_id = task.metadata.get("upload_job_id")
    if upload_job_id:
        upload_repo = UploadJobRepository()
        await asyncio.to_thread(
            upload_repo.mark_item_completed,
            str(task.task_id),
            result_path=result.get("path") or task.metadata.get("mount_path"),
        )
        await asyncio.to_thread(upload_repo.refresh_job_from_items, upload_job_id)

    return UploadCompleteResponse(
        task_id=str(task.task_id),
        status=IngestStatus.COMPLETED,
        path=result.get("path") or task.metadata.get("mount_path"),
    )


@router.post(
    "/upload/complete-batch",
    response_model=UploadCompleteBatchResponse,
)
async def complete_upload_batch(
    request: UploadCompleteBatchRequest,
    s3_service: S3Service = Depends(get_s3_service),
    etl_service: ETLService = Depends(get_etl_service),
    current_user: CurrentUser = Depends(get_current_user),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    """
    Finalize multiple multipart uploads as one project-root product commit.

    Why this exists:
      The single-file ``/upload/complete`` endpoint pays a fixed
      per-commit overhead (negotiate + push RPCs, ~1.5–2s on a warm
      cache). For a folder of 100 files that adds up to 150–200s of
      sequential commits — and 100 nearly-identical entries in the
      audit log. This endpoint collapses that into one ``bulk_write``
      call -> one visible project-root commit.

    Failure model — partial success with HTTP 200:
      The response always carries one ``UploadCompleteItemResult``
      per input item, including failures. We chose this over
      "all-or-nothing 5xx" because:
        * the user has already paid the bandwidth to upload all
          parts; bouncing the whole batch would force them to
          re-upload every file
        * the typical failure mode is "one weird file" (mount path
          collision, ETag mismatch, S3 hiccup), not "everything is
          broken" — partial success degrades gracefully.
      Callers must walk ``items`` and surface failures per-file
      rather than treating the whole response as one transaction.

    Step-by-step:
      1. Per item: validate ownership + state + s3_key/upload_id
         echo. Items that fail validation are recorded immediately
         and excluded from later steps.
      2. Per surviving item: run S3
         ``CompleteMultipartUpload``. Failures here mark the task
         FAILED and are excluded from the bulk push.
      3. Single call to ``finalize_uploads_to_version_batch`` for all
         items that made it past steps 1–2. That helper does the
         CopyObject pre-stage + one project-root ``bulk_write``.
      4. Merge per-item results from each phase into a single
         ordered response, preserving the input ordering.
    """
    # ────────────────────────────────────────────────────────────────
    # Phase 1: validate (auth, state, echo cross-check). Build maps
    # that the later phases consume.
    # ────────────────────────────────────────────────────────────────
    item_results: dict[str, UploadCompleteItemResult] = {}
    eligible: list = []  # items that survive validation

    for item in request.items:
        try:
            task = _authorize_owned_upload_task(
                etl_service.task_repository.get_task(item.task_id),
                current_user=current_user,
                authorization=authorization,
            )
        except (HTTPException, AppException):
            item_results[item.task_id] = UploadCompleteItemResult(
                task_id=item.task_id,
                status=IngestStatus.FAILED,
                error="Task not found or Project write access is no longer available",
            )
            continue

        if task.status != ETLTaskStatus.PENDING:
            item_results[item.task_id] = UploadCompleteItemResult(
                task_id=item.task_id,
                status=IngestStatus.FAILED,
                error=f"Task is not pending (status={task.status.value})",
            )
            continue

        expected_key = task.metadata.get("s3_key")
        expected_upload_id = task.metadata.get("upload_id")
        if expected_key != item.s3_key or expected_upload_id != item.upload_id:
            item_results[item.task_id] = UploadCompleteItemResult(
                task_id=item.task_id,
                status=IngestStatus.FAILED,
                error="s3_key/upload_id mismatch with the task",
            )
            continue
        try:
            _validate_complete_manifest(task.metadata, item.parts)
        except HTTPException as exc:
            item_results[item.task_id] = UploadCompleteItemResult(
                task_id=item.task_id,
                status=IngestStatus.FAILED,
                error=str(exc.detail),
            )
            continue

        eligible.append((item, task))

    # ────────────────────────────────────────────────────────────────
    # Phase 2: per-item S3 CompleteMultipartUpload — run in parallel.
    #
    # Each call is just sending an XML manifest to finalize an
    # already-uploaded object; no bytes flow at this point. The
    # operations are independent (different keys, different upload
    # ids) so there's no ordering constraint. Supabase Storage
    # handles bursts of 5–10 concurrent metadata operations
    # comfortably; we cap concurrency at 8 below as a defensive
    # ceiling for very large folder uploads.
    #
    # Sequential previously cost ~1s per file (the round-trip to
    # Supabase + the size-sanity HEAD afterwards). Going parallel
    # collapses that to ~1× one round-trip regardless of N.
    # ────────────────────────────────────────────────────────────────
    _COMPLETE_PARALLEL_LIMIT = 8
    completed_task_ids_set: set = set()
    completed_lock = asyncio.Lock()

    async def _finalize_one(item, task):
        """Complete one multipart upload + record outcome.

        Returns nothing; mutates ``item_results`` and
        ``completed_task_ids_set`` in place. Designed to be safe
        under ``asyncio.gather`` because each invocation only
        touches its own task record (the task repo is
        Supabase-backed; per-task PATCHes are independent).
        """
        parts = sorted([(p.part_number, p.etag) for p in item.parts], key=lambda x: x[0])
        expected_size = _metadata_int(task.metadata, "size")
        if expected_size == 0 and not parts:
            logger.info(
                "Task %s is zero-byte; skipping multipart complete",
                item.task_id,
            )
        else:
            try:
                await s3_service.complete_multipart_upload(item.s3_key, item.upload_id, parts)
            except Exception as e:
                logger.error(
                    f"complete_multipart_upload failed for task {item.task_id}: {e}",
                    exc_info=True,
                )
                try:
                    if item.upload_id:
                        await s3_service.abort_multipart_upload(item.s3_key, item.upload_id)
                except Exception:
                    pass
                task.mark_failed(f"Failed to finalize multipart upload: {e}")
                task.metadata["error_stage"] = "complete_multipart"
                etl_service.task_repository.update_task(task)
                item_results[item.task_id] = UploadCompleteItemResult(
                    task_id=item.task_id,
                    status=IngestStatus.FAILED,
                    error=f"S3 complete_multipart_upload failed: {e}",
                )
                return

        # Sanity-check size as a non-fatal warning (consistent with
        # the single-file endpoint). Failure here just logs — we don't
        # block the upload on a missing HEAD.
        try:
            meta = await s3_service.get_file_metadata(item.s3_key)
            if expected_size is not None and meta.size != expected_size:
                message = f"Declared size {expected_size} differs from S3 size {meta.size}"
                logger.warning(f"Task {item.task_id}: {message}")
                task.mark_failed(message)
                task.metadata["error_stage"] = "size_mismatch"
                etl_service.task_repository.update_task(task)
                item_results[item.task_id] = UploadCompleteItemResult(
                    task_id=item.task_id,
                    status=IngestStatus.FAILED,
                    error=message,
                )
                return
        except Exception as e:
            logger.warning(f"Task {item.task_id}: head_object after complete failed: {e}")

        async with completed_lock:
            completed_task_ids_set.add(task.task_id)

    sem = asyncio.Semaphore(_COMPLETE_PARALLEL_LIMIT)

    async def _bounded(item, task):
        async with sem:
            await _finalize_one(item, task)

    if eligible:
        await asyncio.gather(
            *(_bounded(item, task) for item, task in eligible),
            return_exceptions=False,  # _finalize_one swallows its own errors
        )

    # Preserve input order in completed_task_ids — the bulk finalize
    # downstream doesn't strictly require it, but it makes the
    # response easier to reason about for paginated UIs.
    completed_task_ids = [
        task.task_id for _, task in eligible if task.task_id in completed_task_ids_set
    ]

    # ────────────────────────────────────────────────────────────────
    # Phase 3: ONE bulk finalize for everything that made it this far.
    # ────────────────────────────────────────────────────────────────
    if completed_task_ids:
        from src.ingest.file.jobs.jobs import finalize_uploads_to_version_batch

        try:
            batch_results = await finalize_uploads_to_version_batch(
                task_ids=completed_task_ids,
                repo=etl_service.task_repository,
                s3=s3_service,
                state_repo=etl_service.state_repo,
            )
        except asyncio.CancelledError:
            # Surface a 504 — same contract as the single-file
            # endpoint, callers know to retry the whole batch.
            raise HTTPException(
                status_code=504,
                detail="Bulk finalize timed out writing to ObjectStore",
            )

        for r in batch_results:
            tid = r["task_id"]
            if r.get("ok"):
                if r.get("skipped"):
                    item_results[tid] = UploadCompleteItemResult(
                        task_id=tid,
                        status=IngestStatus.CANCELLED,
                        path=None,
                    )
                else:
                    item_results[tid] = UploadCompleteItemResult(
                        task_id=tid,
                        status=IngestStatus.COMPLETED,
                        path=r.get("path"),
                    )
            else:
                item_results[tid] = UploadCompleteItemResult(
                    task_id=tid,
                    status=IngestStatus.FAILED,
                    error=r.get("error") or "Finalize failed",
                )

    # ────────────────────────────────────────────────────────────────
    # Assemble the response in the original input order so clients can
    # zip results back to their own indices without bookkeeping.
    # ────────────────────────────────────────────────────────────────
    ordered: list[UploadCompleteItemResult] = []
    upload_repo = UploadJobRepository()
    touched_upload_jobs: set[str] = set()
    for item in request.items:
        result = item_results.get(item.task_id)
        if result is None:
            # Defensive: should never happen, but don't drop the item
            # from the response if some new failure mode slips past.
            result = UploadCompleteItemResult(
                task_id=item.task_id,
                status=IngestStatus.FAILED,
                error="Internal: item not processed",
            )
        task = etl_service.task_repository.get_task(item.task_id)
        upload_job_id = task.metadata.get("upload_job_id") if task else None
        if upload_job_id:
            touched_upload_jobs.add(upload_job_id)
            if result.status == IngestStatus.COMPLETED:
                await asyncio.to_thread(
                    upload_repo.mark_item_completed,
                    item.task_id,
                    result_path=result.path,
                )
            elif result.status == IngestStatus.CANCELLED:
                await asyncio.to_thread(
                    upload_repo.mark_item_cancelled,
                    item.task_id,
                    "Upload skipped",
                )
            elif result.status == IngestStatus.FAILED:
                await asyncio.to_thread(
                    upload_repo.mark_item_failed,
                    item.task_id,
                    result.error or "Upload failed",
                )
        ordered.append(result)

    for upload_job_id in touched_upload_jobs:
        await asyncio.to_thread(upload_repo.refresh_job_from_items, upload_job_id)

    return UploadCompleteBatchResponse(items=ordered)


@router.post("/upload/abort", response_model=UploadAbortResponse)
async def abort_upload(
    request: UploadAbortRequest,
    s3_service: S3Service = Depends(get_s3_service),
    etl_service: ETLService = Depends(get_etl_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Abort an in-flight multipart upload and mark the task cancelled.

    Idempotent: callable even if S3 has already cleaned up the
    multipart upload (we swallow ``NoSuchUpload``-style errors). The
    task transition to CANCELLED still happens so the client sees a
    stable terminal state.
    """
    task = etl_service.task_repository.get_task(request.task_id)
    if not task or (task.created_by is not None and task.created_by != current_user.user_id):
        raise HTTPException(status_code=404, detail="Task not found")

    if request.upload_id:
        try:
            await s3_service.abort_multipart_upload(request.s3_key, request.upload_id)
        except Exception as e:
            # The most common reason this fails is "already aborted/
            # completed", which is fine — we still want to flag the task
            # as cancelled so the UI converges.
            logger.warning(
                f"abort_multipart_upload for task {request.task_id} "
                f"raised (likely already gone): {e}"
            )

    if task.status == ETLTaskStatus.PENDING:
        task.mark_cancelled("Upload aborted by client")
        etl_service.task_repository.update_task(task)
        upload_job_id = task.metadata.get("upload_job_id")
        if upload_job_id:
            upload_repo = UploadJobRepository()
            await asyncio.to_thread(
                upload_repo.mark_item_cancelled,
                str(task.task_id),
                "Upload aborted by client",
            )
            await asyncio.to_thread(upload_repo.refresh_job_from_items, upload_job_id)

    return UploadAbortResponse(task_id=str(task.task_id), cancelled=True)


# === SaaS/URL Submit Endpoint ===
