"""Upload protocol schema regressions."""

from __future__ import annotations

from pydantic import ValidationError

from src.platform.upload.schemas import (
    UploadCompleteBatchRequest,
    UploadInitRequest,
)


def test_upload_init_allows_zero_byte_files() -> None:
    request = UploadInitRequest.model_validate({
        "project_id": "proj_1",
        "files": [{
            "filename": "empty.md",
            "size": 0,
            "content_type": "text/markdown",
            "parent_path": None,
        }],
    })

    assert request.files[0].size == 0


def test_upload_init_rejects_negative_sizes() -> None:
    try:
        UploadInitRequest.model_validate({
            "project_id": "proj_1",
            "files": [{
                "filename": "broken.md",
                "size": -1,
                "content_type": "text/markdown",
            }],
        })
    except ValidationError:
        return

    raise AssertionError("negative upload size should be rejected")


def test_complete_batch_allows_empty_parts_for_zero_byte_files() -> None:
    request = UploadCompleteBatchRequest.model_validate({
        "items": [{
            "task_id": "task_1",
            "s3_key": "projects/proj_1/uploads/user/empty.md",
            "upload_id": "",
            "parts": [],
        }],
    })

    assert request.items[0].parts == []
