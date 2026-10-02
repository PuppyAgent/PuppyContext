"""Upload service API.

This router exposes Upload as a first-class product resource. The legacy
``/ingest/upload/*`` routes remain as compatibility aliases while frontend and
new clients move to ``/upload/*``.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from src.platform.upload.handlers import (
    abort_upload,
    complete_upload,
    complete_upload_batch,
    init_multipart_upload,
    submit_file_ingest,
    upload_part,
)
from src.ingest.schemas import IngestSubmitResponse
from src.platform.upload.schemas import (
    UploadAbortResponse,
    UploadCompleteBatchResponse,
    UploadCompleteResponse,
    UploadInitResponse,
    UploadPartResponse,
)


router = APIRouter(prefix="/upload", tags=["upload"])

router.add_api_route(
    "/submit/file",
    submit_file_ingest,
    methods=["POST"],
    response_model=IngestSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
router.add_api_route(
    "/init",
    init_multipart_upload,
    methods=["POST"],
    response_model=UploadInitResponse,
)
router.add_api_route(
    "/part",
    upload_part,
    methods=["PUT"],
    response_model=UploadPartResponse,
)
router.add_api_route(
    "/complete",
    complete_upload,
    methods=["POST"],
    response_model=UploadCompleteResponse,
)
router.add_api_route(
    "/complete-batch",
    complete_upload_batch,
    methods=["POST"],
    response_model=UploadCompleteBatchResponse,
)
router.add_api_route(
    "/abort",
    abort_upload,
    methods=["POST"],
    response_model=UploadAbortResponse,
)

