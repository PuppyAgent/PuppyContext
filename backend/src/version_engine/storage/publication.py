"""Cache-independent, typed and bounded Git closure verification.

Must run under a durable publication pin. This module never issues a receipt
or activates repository authority; only the admitted transaction service may
seal the verified manifest and publish its roots.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.version_engine.storage.object_store import StorageBackend
from src.version_engine.write_engine.git_object_format import (
    decode_object,
    hash_object,
    object_id_bytes,
)
from src.version_engine.write_engine.git_object_graph import object_edges


class ClosureVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedObject:
    kind: str
    size: int
    body_sha256: str
    edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ClosureManifest:
    object_format: str
    roots: Mapping[str, str]
    objects: Mapping[str, VerifiedObject]
    digest: str
    total_bytes: int

    def root_details(self) -> dict:
        result = {}
        for oid, kind in self.roots.items():
            target, visited = oid, set()
            while self.objects[target].kind == "tag":
                if target in visited:
                    raise ClosureVerificationError("tag cycle")
                visited.add(target)
                target = self.objects[target].edges[0][0]
            result[oid] = {"kind": kind, "peeled_oid": target if kind == "tag" else None}
        return result


class ClosureVerifier:
    def __init__(
        self, backend: StorageBackend, *, object_format: str = "sha1",
        max_objects: int = 1_000_000, max_bytes: int = 8 * 1024**3,
    ):
        self.backend = backend
        self.object_format = object_format
        self.width = object_id_bytes(object_format) * 2
        self.max_objects = max_objects
        self.max_bytes = max_bytes

    def verify(
        self, roots: Mapping[str, str | None], *, progress: Callable[[], None] | None = None,
        on_object: Callable[[str, bytes], None] | None = None,
    ) -> ClosureManifest:
        if not roots:
            raise ClosureVerificationError("empty publication closure")
        records: dict[str, VerifiedObject] = {}
        stack = list(roots.items())
        total_bytes = 0
        while stack:
            oid, expected = stack.pop()
            if (len(oid) != self.width or not set(oid) <= set("0123456789abcdef")
                    or oid == "0" * self.width):
                raise ClosureVerificationError("invalid closure object id")
            if oid in records:
                if expected is not None and records[oid].kind != expected:
                    raise ClosureVerificationError("closure object type mismatch")
                continue
            if len(records) >= self.max_objects:
                raise ClosureVerificationError("closure object budget exceeded")
            if progress is not None:
                progress()
            try:
                loose = self.backend.get_durable(oid)
                kind, body = decode_object(loose, max_bytes=self.max_bytes - total_bytes)
                if expected is not None and kind != expected:
                    raise ValueError("closure object type mismatch")
                if hash_object(kind, body, object_format=self.object_format) != oid:
                    raise ValueError("closure object hash mismatch")
                edges = object_edges(kind, body, object_format=self.object_format)
            except Exception as exc:
                raise ClosureVerificationError(f"cannot verify {oid}: {exc}") from exc
            total_bytes += len(body)
            digest = hashlib.sha256(f"{kind} {len(body)}\0".encode("ascii") + body).hexdigest()
            records[oid] = VerifiedObject(kind, len(body), digest, tuple(edges))
            if on_object is not None:
                on_object(oid, loose)
            stack.extend(edges)
        roots = {oid: records[oid].kind for oid in roots}
        encoded = json.dumps({
            "version": 1, "object_format": self.object_format, "roots": dict(roots),
            "objects": [(oid, record.kind, record.size, record.body_sha256)
                        for oid, record in sorted(records.items())],
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
        return ClosureManifest(
            self.object_format, MappingProxyType(dict(roots)), MappingProxyType(records),
            hashlib.sha256(encoded).hexdigest(), total_bytes,
        )
