"""Git-format content-addressed object store owned by PuppyOne."""

from __future__ import annotations

import abc
import asyncio
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.write_engine.git_object_format import (
    EMPTY_TREE_CONTENT,
    decode_object,
    encode_object,
    hash_object,
)


class StorageBackend(abc.ABC):
    @property
    def publication_project_id(self) -> str | None:
        """Namespace binding when the backend addresses multiple Projects."""
        return None

    def pinned_reader(self, snapshot):
        """Optional bounded bulk metadata read; filesystem backends need none."""
        return self

    def get_durable(self, h: str) -> bytes:
        """Explicit physical read capability; unknown/caching backends fail closed."""
        raise NotImplementedError("backend does not provide durable object readback")

    def put_durable(self, h: str, loose_bytes: bytes) -> None:
        """Write-through; caching backends must override to bypass staging."""
        self.put(h, loose_bytes)

    @abc.abstractmethod
    def get(self, h: str) -> bytes:
        """Return Git loose-object bytes for object id ``h``."""

    @abc.abstractmethod
    def put(self, h: str, loose_bytes: bytes) -> None:
        """Store Git loose-object bytes under object id ``h``."""

    @abc.abstractmethod
    def exists(self, h: str) -> bool: ...

    def exists_many(self, hashes: list[str]) -> set[str]:
        return {h for h in hashes if self.exists(h)}

    @abc.abstractmethod
    def all_hashes(self) -> list[str]: ...

    @abc.abstractmethod
    def count(self) -> tuple[int, int]: ...

    @abc.abstractmethod
    def delete(self, h: str) -> bool: ...


class FileSystemBackend(StorageBackend):
    """Git loose-object filesystem layout: ``objects/aa/bb...``."""

    def __init__(self, objects_dir: Path):
        self.dir = objects_dir

    def _path_for(self, h: str) -> Path:
        return self.dir / h[:2] / h[2:]

    def get(self, h: str) -> bytes:
        path = self._path_for(h)
        if not path.exists():
            raise ObjectNotFoundError(f"object not found: {h}")
        return path.read_bytes()

    def put(self, h: str, loose_bytes: bytes) -> None:
        path = self._path_for(h)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(loose_bytes)
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def get_durable(self, h: str) -> bytes:
        return self.get(h)

    def exists(self, h: str) -> bool:
        return self._path_for(h).exists()

    def all_hashes(self) -> list[str]:
        result: list[str] = []
        if not self.dir.exists():
            return result
        for shard in sorted(self.dir.iterdir()):
            if shard.is_dir() and len(shard.name) == 2:
                for obj in sorted(shard.iterdir()):
                    result.append(shard.name + obj.name)
        return result

    def count(self) -> tuple[int, int]:
        n, size = 0, 0
        if not self.dir.exists():
            return 0, 0
        for shard in self.dir.iterdir():
            if shard.is_dir():
                for obj in shard.iterdir():
                    n += 1
                    size += obj.stat().st_size
        return n, size

    def delete(self, h: str) -> bool:
        path = self._path_for(h)
        if not path.exists():
            return False
        path.unlink()
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        return True


class ObjectStore:
    """High-level store for Git loose objects."""

    def __init__(
        self, objects_dir: Path, backend: StorageBackend | None = None,
        *, object_format: str = "sha1",
    ):
        self._empty_tree_id, self._empty_tree_loose = encode_object(
            "tree", EMPTY_TREE_CONTENT, object_format=object_format,
        )
        self._object_format = object_format
        self.dir = objects_dir
        self._backend = backend or FileSystemBackend(objects_dir)

    @property
    def object_format(self) -> str:
        """Repository identity, not a format guessed from an incoming object."""
        return self._object_format

    def _put_typed(self, obj_type: str, content: bytes) -> str:
        sha1, loose = encode_object(obj_type, content, object_format=self.object_format)
        if sha1 == self._empty_tree_id:
            return sha1
        self._backend.put(sha1, loose)
        return sha1

    def put_blob(self, data: bytes) -> str:
        return self._put_typed("blob", data)

    def put_tree(self, content: bytes) -> str:
        return self._put_typed("tree", content)

    def put_commit(self, content: bytes) -> str:
        return self._put_typed("commit", content)

    def get_object(self, sha1: str) -> tuple[str, bytes]:
        if sha1 == self._empty_tree_id:
            return "tree", EMPTY_TREE_CONTENT
        return self._decode_verified(
            sha1, self._backend.get(sha1), object_format=self.object_format,
        )

    @staticmethod
    def _decode_verified(
        sha1: str, loose: bytes, *, object_format: str = "sha1",
    ) -> tuple[str, bytes]:
        try:
            obj_type, content = decode_object(loose)
        except Exception as exc:
            raise ObjectNotFoundError(f"object corrupt: {sha1} ({exc})") from exc
        actual = hash_object(obj_type, content, object_format=object_format)
        if actual != sha1:
            raise ObjectNotFoundError(f"object corrupt: expected {sha1}, got {actual}")
        return obj_type, content

    def get_objects_many(self, sha1s: list[str]) -> dict[str, tuple[str, bytes]]:
        """Batch transport only; identical hash verification to single reads."""
        return {
            sha1: self._decode_verified(sha1, loose, object_format=self.object_format)
            for sha1, loose in self.get_loose_many(sha1s).items()
        }

    def put_loose(self, sha1: str, loose_bytes: bytes) -> None:
        if sha1 == self._empty_tree_id:
            return
        self._backend.put(sha1, loose_bytes)

    def get_loose(self, sha1: str) -> bytes:
        if sha1 == self._empty_tree_id:
            return self._empty_tree_loose
        return self._backend.get(sha1)

    def get_loose_many(self, sha1s: list[str]) -> dict[str, bytes]:
        unique = list(dict.fromkeys(sha1s))
        out: dict[str, bytes] = {}
        rest: list[str] = []
        for sha1 in unique:
            if sha1 == self._empty_tree_id:
                out[sha1] = self._empty_tree_loose
            else:
                rest.append(sha1)
        getter = getattr(self._backend, "get_many", None)
        if callable(getter) and rest:
            out.update(getter(rest))
            rest = [sha1 for sha1 in rest if sha1 not in out]
        for sha1 in rest:
            out[sha1] = self._backend.get(sha1)
        return out

    def put(self, data: bytes) -> str:
        return self.put_blob(data)

    def get(self, h: str) -> bytes:
        _obj_type, content = self.get_object(h)
        return content

    def exists(self, h: str) -> bool:
        if h == self._empty_tree_id:
            return True
        return self._backend.exists(h)

    def exists_many(self, hashes: list[str]) -> set[str]:
        existing = {h for h in hashes if h == self._empty_tree_id}
        rest = [h for h in hashes if h != self._empty_tree_id]
        if rest:
            existing.update(self._backend.exists_many(rest))
        return existing

    def all_hashes(self) -> list[str]:
        return self._backend.all_hashes()

    def count(self) -> tuple[int, int]:
        return self._backend.count()

    def gc(self, reachable: set[str]) -> int:
        deleted = 0
        for h in self.all_hashes():
            if h not in reachable and self._backend.delete(h):
                deleted += 1
        return deleted

    async def async_put(self, data: bytes) -> str:
        sha1, loose = encode_object("blob", data, object_format=self.object_format)
        if sha1 == self._empty_tree_id:
            return sha1
        async_put = getattr(self._backend, "async_put", None)
        if callable(async_put):
            await async_put(sha1, loose)
        else:
            await asyncio.to_thread(self._backend.put, sha1, loose)
        return sha1

    async def async_get(self, h: str) -> bytes:
        return await asyncio.to_thread(self.get, h)

    async def async_exists(self, h: str) -> bool:
        return await asyncio.to_thread(self.exists, h)

    async def async_all_hashes(self) -> list[str]:
        return await asyncio.to_thread(self.all_hashes)

    async def async_count(self) -> tuple[int, int]:
        return await asyncio.to_thread(self.count)

    async def async_put_loose(self, h: str, loose: bytes) -> None:
        if h == self._empty_tree_id:
            return
        async_put = getattr(self._backend, "async_put", None)
        if callable(async_put):
            await async_put(h, loose)
        else:
            await asyncio.to_thread(self._backend.put, h, loose)

    async def async_get_loose(self, h: str) -> bytes:
        return await asyncio.to_thread(self.get_loose, h)


@contextmanager
def stage_object_writes(store_or_backend):
    """Stage immutable object writes when the backend supports batching.

    This is intentionally defined at the object-store boundary so callers such
    as the Write Engine and Git transport do not depend on a concrete S3
    implementation. Backends that can batch expose ``stage_object_writes()``;
    simpler filesystem/test backends naturally fall through to immediate
    writes.
    """

    backend = getattr(store_or_backend, "_backend", store_or_backend)
    starter = getattr(backend, "stage_object_writes", None)
    if not callable(starter):
        yield None
        return
    with starter() as batch:
        yield batch
