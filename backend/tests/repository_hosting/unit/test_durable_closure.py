"""Physical readback, not cached existence, is a publication prerequisite."""

import hashlib
from pathlib import Path

import pytest

from src.version_engine.storage.backends.s3 import CachedStorageBackend
from src.version_engine.storage.object_store import FileSystemBackend, ObjectStore
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_manifest_verifies_all_native_types_raw_bytes_and_external_gitlinks(tmp_path, format):
    git = Git.init(tmp_path / "native", format=format)
    commit = git.commit({"raw.bin": bytes(range(256)), ".gitmodules": b"external"})
    git.run("update-index", "--add", "--cacheinfo", "160000," + "a"*len(commit) + ",external")
    git.run("commit", "-m", "gitlink")
    git.run("tag", "-a", "tag", "-m", "opaque tag")
    objects = git.objects()
    backend = FileSystemBackend(tmp_path / "physical")
    for oid, (kind, body) in objects.items():
        actual, loose = encode_object(kind, body, object_format=format)
        assert actual == oid
        backend.put(oid, loose)
    roots = {oid: kind for oid, (kind, _) in objects.items()}
    manifest = ClosureVerifier(backend, object_format=format).verify(roots)
    assert set(manifest.objects) == set(objects)
    assert manifest.roots == roots
    assert manifest.digest == ClosureVerifier(backend, object_format=format).verify(dict(reversed(list(roots.items())))).digest
    assert len(manifest.digest) == 64
    assert manifest.total_bytes == sum(len(body) for _, body in objects.values())


def test_cached_object_is_not_physical_proof(tmp_path):
    inner = FileSystemBackend(tmp_path / "objects")
    cache = CachedStorageBackend(inner)
    store = ObjectStore(tmp_path / "unused", backend=cache)
    oid = store.put_blob(b"acknowledge only durable data")
    assert store.get(oid) == b"acknowledge only durable data"
    inner.delete(oid)
    assert store.get(oid) == b"acknowledge only durable data"
    with pytest.raises(Exception, match="object not found"):
        ClosureVerifier(cache).verify({oid: "blob"})


def test_staged_object_is_not_physical_proof(tmp_path):
    cache = CachedStorageBackend(FileSystemBackend(tmp_path / "objects"))
    store = ObjectStore(tmp_path / "unused", backend=cache)
    with cache.stage_object_writes():
        oid = store.put_blob(b"unflushed")
        assert store.exists(oid)
        with pytest.raises(Exception, match="object not found"):
            ClosureVerifier(cache).verify({oid: "blob"})


@pytest.mark.parametrize("fault", ["type", "body", "edge", "object_limit", "byte_limit"])
def test_invalid_or_over_budget_closure_cannot_receive_a_manifest(tmp_path, fault):
    backend = FileSystemBackend(tmp_path / "objects")
    blob, loose = encode_object("blob", b"contents")
    backend.put(blob, loose)
    roots = {blob: "tree" if fault == "type" else "blob"}
    kwargs = {}
    if fault == "body":
        Path(backend.dir / blob[:2] / blob[2:]).write_bytes(encode_object("blob", b"wrong")[1])
    if fault == "edge":
        oid, loose = encode_object("commit", b"tree " + b"c"*40 + b"\n\nmissing tree\n")
        backend.put(oid, loose)
        roots = {oid: "commit"}
    if fault == "object_limit":
        kwargs["max_objects"] = 0
    if fault == "byte_limit":
        kwargs["max_bytes"] = 1
    with pytest.raises((ValueError, RuntimeError)):
        ClosureVerifier(backend, **kwargs).verify(roots)


def test_manifest_digest_covers_contents_and_format_not_compression(tmp_path):
    backend = FileSystemBackend(tmp_path / "objects")
    oid, loose = encode_object("blob", b"hello")
    backend.put(oid, loose)
    manifest = ClosureVerifier(backend).verify({oid: "blob"})
    assert manifest.objects[oid].body_sha256 == hashlib.sha256(b"blob 5\0hello").hexdigest()
    import zlib
    (backend.dir / oid[:2] / oid[2:]).write_bytes(zlib.compress(b"blob 5\0hello", level=0))
    assert ClosureVerifier(backend).verify({oid: "blob"}).digest == manifest.digest
