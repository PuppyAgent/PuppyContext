"""Legacy upload prestaging must not bypass native publication admission."""
import zlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.infra.s3.exceptions import S3FileNotFoundError, S3OperationError
from src.platform.upload.jobs import stage_blob_from_s3
from src.version_engine.write_engine.git_object_format import encode_object

pytestmark = pytest.mark.hosting_component


class Storage:
    bucket_name = "fixture"

    def __init__(self, raw=b"original input"):
        self.raw = raw
        self.oid, self.loose = encode_object("blob", raw)
        self.key = f"version/project/objects/{self.oid[:2]}/{self.oid[2:]}"
        self.objects = {"input": raw}
        self.calls = []
        self.read_error = None
        self.damage_upload = False
        self.closed = []
        self.client = SimpleNamespace(head_object=self.head)

    def head(self, **kwargs):
        self.calls.append(("head", kwargs["Key"]))
        return {"ContentLength": len(self.raw)}

    async def download_file_stream(self, key, chunk_size):
        self.calls.append(("get", key))
        if key == self.key and self.read_error:
            raise self.read_error
        if key not in self.objects:
            raise S3FileNotFoundError(key)
        body = self.objects[key]
        try:
            for offset in range(0, len(body), 7):
                yield body[offset:offset + 7]
        finally:
            self.closed.append(key)

    async def get_file_metadata(self, key):
        if key not in self.objects:
            raise S3FileNotFoundError(key)
        return SimpleNamespace(size=len(self.objects[key]))

    async def upload_file(self, key, content, content_type=None):
        self.calls.append(("put", key))
        self.objects[key] = b"damaged" if self.damage_upload else content


class Admission:
    def __init__(self, metadata=None):
        self.metadata = metadata
        self.active = False
        self.reads = 0
        self.on_enter = lambda: None
        self.on_read = lambda: None

    @asynccontextmanager
    async def lease(self, project_id, channel):
        assert project_id == "project"
        self.on_enter()
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def repository_metadata(self, project_id):
        assert self.active
        assert project_id == "project"
        self.reads += 1
        self.on_read()
        if isinstance(self.metadata, Exception):
            raise self.metadata
        return self.metadata


def stage(storage, admission):
    return stage_blob_from_s3(
        storage, project_id="project", src_key="input",
        repo_manager=admission, write_lease_factory=admission.lease,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_native_upload_prestaging_is_rejected_before_any_storage_io(object_format):
    storage = Storage()
    admission = Admission({"project_id": "project", "authority": "native", "object_format": object_format})
    with pytest.raises(RuntimeError, match=r"native.*staging"):
        await stage(storage, admission)
    assert storage.calls == []
    assert not admission.active


@pytest.mark.asyncio
async def test_upload_authority_is_read_after_lease_wait():
    storage, admission = Storage(), Admission()
    admission.on_enter = lambda: setattr(admission, "metadata", {"authority": "native"})
    with pytest.raises(RuntimeError, match=r"native.*staging"):
        await stage(storage, admission)
    assert storage.calls == []


@pytest.mark.asyncio
async def test_upload_authority_failure_is_not_legacy_absence():
    storage, admission = Storage(), Admission(RuntimeError("metadata unavailable"))
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        await stage(storage, admission)
    assert storage.calls == []


@pytest.mark.asyncio
async def test_upload_rechecks_authority_after_input_io():
    storage, admission = Storage(), Admission()

    def change():
        if admission.reads == 2:
            admission.metadata = {"authority": "native"}

    admission.on_read = change
    with pytest.raises(RuntimeError, match=r"native.*staging"):
        await stage(storage, admission)
    assert storage.calls == [("head", "input"), ("get", "input")]
    assert storage.closed == ["input"]


@pytest.mark.asyncio
async def test_upload_rechecks_authority_after_destination_io():
    storage, admission = Storage(), Admission()

    def change():
        if admission.reads == 3:
            admission.metadata = {"authority": "native"}

    admission.on_read = change
    with pytest.raises(RuntimeError, match=r"native.*staging"):
        await stage(storage, admission)
    assert ("put", storage.key) not in storage.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("resident", ["same_length", "raw", "truncated", "trailing", "bomb"])
async def test_legacy_upload_verifies_content_not_encoded_length(resident):
    storage, admission = Storage(), Admission()
    storage.objects[storage.key] = {
        "same_length": b"x" * len(storage.loose),
        "raw": storage.raw,
        "truncated": storage.loose[:-1],
        "trailing": storage.loose + b"garbage",
        "bomb": zlib.compress(b"x" * 1024 * 1024),
    }[resident]
    result = await stage(storage, admission)
    assert result.hash == storage.oid
    assert result.size == len(storage.raw)
    assert storage.objects[storage.key] == storage.loose
    assert storage.calls.count(("put", storage.key)) == 1
    assert storage.calls.count(("get", storage.key)) == 2
    assert storage.closed == ["input", storage.key, storage.key]


@pytest.mark.asyncio
async def test_valid_different_encoding_is_reused_without_put():
    storage, admission = Storage(), Admission({"authority": "shadow"})
    alternative = zlib.compress(b"blob 14\0" + storage.raw, level=0)
    assert alternative != storage.loose
    storage.objects[storage.key] = alternative
    result = await stage(storage, admission)
    assert result.hash == storage.oid
    assert storage.objects[storage.key] == alternative
    assert ("put", storage.key) not in storage.calls


@pytest.mark.asyncio
async def test_uncertain_destination_read_never_becomes_permission_to_put():
    storage, admission = Storage(), Admission()
    storage.read_error = S3OperationError("unavailable")
    with pytest.raises(S3OperationError):
        await stage(storage, admission)
    assert ("put", storage.key) not in storage.calls


@pytest.mark.asyncio
async def test_verification_budget_exhaustion_is_not_proof_of_invalid_bytes():
    storage, admission = Storage(), Admission()
    # Valid empty non-final deflate blocks, not a decompression bomb/corruption.
    padded = storage.loose[:2] + b"\x00\x00\x00\xff\xff" * 14_000 + storage.loose[2:]
    assert zlib.decompress(padded) == b"blob 14\0" + storage.raw
    storage.objects[storage.key] = padded
    with pytest.raises(S3OperationError, match="verification byte budget exceeded"):
        await stage(storage, admission)
    assert ("put", storage.key) not in storage.calls
    assert storage.objects[storage.key] == padded
    assert storage.closed == ["input", storage.key]


@pytest.mark.asyncio
async def test_upload_does_not_return_a_reference_without_physical_verification():
    storage, admission = Storage(), Admission()
    storage.damage_upload = True
    with pytest.raises(S3OperationError, match="staged Git blob verification failed"):
        await stage(storage, admission)
    assert storage.objects["input"] == storage.raw
    assert not admission.active


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"", b"x" * 200_000])
async def test_empty_and_multichunk_decoded_objects_are_verified(raw):
    storage, admission = Storage(raw), Admission()
    storage.objects[storage.key] = storage.loose
    result = await stage(storage, admission)
    assert result.hash == storage.oid and result.size == len(raw)
    assert ("put", storage.key) not in storage.calls
    assert storage.closed == ["input", storage.key]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [-1, True, "14"])
async def test_invalid_source_metadata_is_not_an_empty_upload(size):
    storage, admission = Storage(), Admission()
    storage.client.head_object = lambda **_: {"ContentLength": size}
    with pytest.raises(S3OperationError, match="invalid upload staging source size"):
        await stage(storage, admission)
    assert storage.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [b"", b"unexpected longer source"])
async def test_source_size_changes_stop_before_canonical_io_and_close_input(changed):
    storage, admission = Storage(), Admission()
    storage.objects["input"] = changed
    with pytest.raises(S3OperationError, match="S3 object"):
        await stage(storage, admission)
    assert storage.calls == [("head", "input"), ("get", "input")]
    assert storage.closed == ["input"]
    assert not admission.active
