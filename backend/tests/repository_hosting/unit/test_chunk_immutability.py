"""A late producer must not overwrite another placement of the same Git OID."""
from __future__ import annotations

import hashlib
import json
import zlib
from types import SimpleNamespace

import pytest

from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.storage.backends.s3 import ObjectLocation, S3StorageBackend, _run_async
from src.version_engine.storage.chunk_manifest import chunk_upload_plan
from src.version_engine.storage.io_strategy import IOStorageStrategy
from src.version_engine.write_engine.git_object_format import encode_object

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("different_compression", [False, True])
def test_chunk_placement_keys_never_name_different_bytes(different_compression):
    body = bytes(range(256)) * 16
    oid, encoded = encode_object("blob", body)
    other = zlib.compress(zlib.decompress(encoded), level=1) if different_compression else encoded
    first = S3StorageBackend(None, "chunk-project", supabase=SimpleNamespace(),
                             io_strategy=IOStorageStrategy(64, 32))
    second = S3StorageBackend(None, "chunk-project", supabase=SimpleNamespace(),
                              io_strategy=IOStorageStrategy(64, 64))
    old_uploads, old_location = first._chunked_object_upload_plan(oid, encoded)
    new_uploads, new_location = second._chunked_object_upload_plan(oid, other)
    old = {key: value for key, value, _mime in old_uploads}
    new = {key: value for key, value, _mime in new_uploads}
    assert old_location["pack_key"] != new_location["pack_key"]
    # Existing readers accept version 1 and follow explicit keys. Do not force
    # a reader/writer rollout just to change physical placement identities.
    assert json.loads(old[old_location["pack_key"].removeprefix("chunked:")])["version"] == 1
    assert json.loads(new[new_location["pack_key"].removeprefix("chunked:")])["version"] == 1
    for key in old.keys() & new.keys():
        assert old[key] == new[key], f"mutable physical placement: {key}"


class MemoryS3:
    def __init__(self, objects):
        self.objects = objects
        self.reads = []
        self.deletes = []

    async def download_file(self, key):
        self.reads.append(key)
        return self.objects[key]

    async def delete_file(self, key):
        self.deletes.append(key)
        raise AssertionError("invalid manifest must not authorize any deletion")


def legacy_placement():
    oid, loose = encode_object("blob", b"legacy chunk content")
    root = f"version/chunk-project/object-bundles/chunked/{oid[:2]}/{oid}"
    part = root + "/part-000001"
    manifest_key = root + ".json"
    manifest = {"version": 1, "object_id": oid, "size_bytes": len(loose),
                "chunks": [{"key": part, "offset_bytes": 0, "size_bytes": len(loose)}]}
    return oid, loose, manifest_key, manifest, {part: loose}


def test_legacy_chunk_reads_remain_compatible_but_do_not_prove_native_durability(monkeypatch):
    oid, loose, key, manifest, objects = legacy_placement()
    objects[key] = json.dumps(manifest).encode()
    s3 = MemoryS3(objects)
    location = ObjectLocation("chunked:" + key, 0, len(loose))
    monkeypatch.setattr(S3StorageBackend, "_lookup_object_location", lambda _self, _oid: location)
    backend = S3StorageBackend(s3, "chunk-project")
    assert backend.get(oid) == loose
    s3.reads.clear()
    with pytest.raises(StorageWriteError, match="requires immutable chunk placements"):
        backend.get_durable(oid)
    assert s3.reads == [key]


@pytest.mark.parametrize("mutation", ["foreign-part", "overlap", "gap", "size", "shape", "version", "object"])
@pytest.mark.parametrize("operation", ["read", "delete"])
def test_invalid_chunk_manifest_is_rejected_before_part_io(mutation, operation):
    oid, loose, _legacy_key, _manifest, _objects = legacy_placement()
    uploads, key = chunk_upload_plan("version/chunk-project/object-bundles", oid, loose, 8)
    objects = {k: body for k, body, _mime in uploads}
    manifest = json.loads(objects[key])
    if mutation == "foreign-part":
        manifest["chunks"][-1]["key"] = manifest["chunks"][-1]["key"].replace("chunk-project", "other-project")
    elif mutation == "overlap":
        manifest["chunks"][1]["offset_bytes"] = 0
    elif mutation == "gap":
        manifest["chunks"][1]["offset_bytes"] += 1
    elif mutation == "size":
        manifest["size_bytes"] += 1
    elif mutation == "shape":
        manifest["chunks"][-1]["size_bytes"] = "8"
    elif mutation == "version":
        manifest["version"] = True
    else:
        manifest["object_id"] = "0" * 40
    raw = json.dumps(manifest).encode()
    # Re-address the malformed manifest: this tests its structure/namespace,
    # rather than merely failing because its own digest changed.
    key = key.rsplit("manifest-", 1)[0] + "manifest-" + hashlib.sha256(raw).hexdigest() + ".json"
    objects[key] = raw
    s3 = MemoryS3(objects)
    backend = S3StorageBackend(s3, "chunk-project")
    location = ObjectLocation("chunked:" + key, 0, len(loose))
    with pytest.raises(StorageWriteError, match="invalid chunk manifest"):
        if operation == "read":
            _run_async(backend._async_get_chunked_object_at(oid, location))
        else:
            backend._delete_chunked(oid, location)
    assert s3.reads == [key]
    assert not s3.deletes


@pytest.mark.parametrize("operation", ["read", "delete"])
def test_foreign_manifest_location_is_rejected_before_manifest_io(operation):
    oid, loose, key, _manifest, objects = legacy_placement()
    key = key.replace("chunk-project", "other-project")
    s3 = MemoryS3(objects)
    backend = S3StorageBackend(s3, "chunk-project")
    location = ObjectLocation("chunked:" + key, 0, len(loose))
    with pytest.raises(StorageWriteError, match="manifest namespace"):
        if operation == "read":
            _run_async(backend._async_get_chunked_object_at(oid, location))
        else:
            backend._delete_chunked(oid, location)
    assert not s3.reads and not s3.deletes


@pytest.mark.parametrize("failure", ["unavailable", "bad-json"])
def test_unreadable_manifest_does_not_fall_back_to_destructive_listing(failure):
    oid, loose, key, _manifest, _objects = legacy_placement()

    class Unreadable(MemoryS3):
        async def download_file(self, key):
            self.reads.append(key)
            if failure == "unavailable":
                raise ConnectionError("manifest service unavailable")
            return b"not JSON"

        async def list_files(self, **_kwargs):
            raise AssertionError("only an explicitly absent manifest permits listing")

    s3 = Unreadable({})
    backend = S3StorageBackend(s3, "chunk-project")
    error = ConnectionError if failure == "unavailable" else StorageWriteError
    with pytest.raises(error):
        backend._delete_chunked(oid, ObjectLocation("chunked:" + key, 0, len(loose)))
    assert s3.reads == [key] and not s3.deletes


def test_same_size_corrupt_part_fails_its_physical_digest():
    oid, loose = encode_object("blob", b"immutable chunk digest")
    uploads, key = chunk_upload_plan("version/chunk-project/object-bundles", oid, loose, 8)
    objects = {k: body for k, body, _mime in uploads}
    first_key = uploads[0][0]
    objects[first_key] = b"x" * len(objects[first_key])
    backend = S3StorageBackend(MemoryS3(objects), "chunk-project")
    with pytest.raises(StorageWriteError, match="chunk digest mismatch"):
        _run_async(backend._async_get_chunked_object_at(
            oid, ObjectLocation("chunked:" + key, 0, len(loose)),
        ))
