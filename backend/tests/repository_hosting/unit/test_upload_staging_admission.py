"""Upload input remains private until the native writer admits publication."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.infra.s3.exceptions import S3OperationError
from src.platform.upload.jobs import stage_blob_from_s3
from src.version_engine.write_engine.git_object_format import hash_object

pytestmark = pytest.mark.hosting_component
SOURCE = "projects/project/uploads/input"


class Storage:
    bucket_name = "fixture"

    def __init__(self, raw=b"original input"):
        self.raw = raw
        self.size = len(raw)
        self.calls = []
        self.closed = False
        self.failure = None
        self.client = SimpleNamespace(head_object=self.head)

    def head(self, **kwargs):
        self.calls.append(("head", kwargs["Key"]))
        return {"ContentLength": self.size}

    async def download_file_stream(self, key, chunk_size):
        self.calls.append(("get", key))
        assert chunk_size == 1024**2
        try:
            for offset in range(0, len(self.raw), chunk_size):
                yield self.raw[offset : offset + chunk_size]
            if self.failure:
                raise self.failure
        finally:
            self.closed = True

    async def upload_file(self, *_args, **_kwargs):
        pytest.fail("staging wrote canonical storage before publication")


def stage(storage, **kwargs):
    return stage_blob_from_s3(storage, project_id="project", src_key=SOURCE, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", [b"", b"bytes\x00", b"x" * (1024**2 + 17)], ids=["empty", "binary", "multichunk"]
)
async def test_staging_returns_verified_private_body_without_canonical_io(raw):
    storage = Storage(raw)
    manager = Mock()
    ref = await stage(storage, repo_manager=manager, expected_size=len(raw))
    assert ref.hash == hash_object("blob", raw)
    assert ref.size == len(raw) and ref.content == raw
    assert storage.calls == [("head", SOURCE), ("get", SOURCE)]
    assert storage.closed
    assert not manager.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [-1, True, "14", None])
async def test_invalid_metadata_fails_before_body_io(size):
    storage = Storage()
    storage.size = size
    with pytest.raises(S3OperationError, match="invalid upload staging source size"):
        await stage(storage)
    assert storage.calls == [("head", SOURCE)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 100])
async def test_size_drift_closes_stream_without_publication(size):
    storage = Storage()
    storage.size = size
    with pytest.raises(S3OperationError, match=r"size changed|truncated"):
        await stage(storage)
    assert storage.closed
    assert storage.calls == [("head", SOURCE), ("get", SOURCE)]


@pytest.mark.asyncio
async def test_reservation_size_drift_and_body_limit_fail_before_download():
    storage = Storage()
    with pytest.raises(S3OperationError, match="size changed"):
        await stage(storage, expected_size=1)
    storage.size = 64 * 1024**2 + 1
    with pytest.raises(S3OperationError, match="body limit"):
        await stage(storage)
    assert all(call[0] == "head" for call in storage.calls)


@pytest.mark.asyncio
async def test_input_failure_propagates_and_closes_stream():
    storage = Storage()
    storage.failure = RuntimeError("source unavailable")
    with pytest.raises(RuntimeError, match="source unavailable"):
        await stage(storage)
    assert storage.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["projects/other/uploads/input", "input", "version/project/objects/ab/123"]
)
async def test_foreign_or_non_upload_source_rejected_before_io(key):
    storage = Storage()
    with pytest.raises(PermissionError):
        await stage_blob_from_s3(storage, project_id="project", src_key=key)
    assert storage.calls == []
