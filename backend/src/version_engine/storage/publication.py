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
    logical_bytes: int | None = None
    max_blob_bytes: int | None = None


@dataclass(frozen=True)
class ClosureManifest:
    object_format: str
    roots: Mapping[str, str]
    objects: Mapping[str, VerifiedObject]
    digest: str
    total_bytes: int
    new_objects: tuple[str, ...] | None = None
    resolve: Callable[[str], VerifiedObject | None] | None = None

    def object(self, oid):
        record = self.objects.get(oid)
        if record is None and self.resolve is not None:
            record = self.resolve(oid)
        if record is None:
            raise ClosureVerificationError("missing verified object dependency")
        return record

    def root_details(self) -> dict:
        result = {}
        for oid, kind in self.roots.items():
            target, visited = oid, set()
            while self.object(target).kind == "tag":
                if target in visited:
                    raise ClosureVerificationError("tag cycle")
                visited.add(target)
                target = self.object(target).edges[0][0]
            result[oid] = {"kind": kind, "peeled_oid": target if kind == "tag" else None}
        return result


class ClosureVerifier:
    def __init__(
        self,
        backend: StorageBackend,
        *,
        object_format: str = "sha1",
        max_objects: int = 1_000_000,
        max_bytes: int = 8 * 1024**3,
    ):
        self.backend = backend
        self.object_format = object_format
        self.width = object_id_bytes(object_format) * 2
        self.max_objects = max_objects
        self.max_bytes = max_bytes
        self.proofs = None

    def verify(
        self,
        roots: Mapping[str, str | None],
        *,
        progress: Callable[[], None] | None = None,
        on_object: Callable[[str, bytes], None] | None = None,
    ) -> ClosureManifest:
        if self.proofs is not None:
            return self._incremental(roots, progress=progress, on_object=on_object)
        if not roots:
            raise ClosureVerificationError("empty publication closure")
        factory = getattr(self.backend, "durable_readback", None)
        reader = factory() if callable(factory) else self.backend
        prefetch = getattr(reader, "prefetch_durable", None)
        records: dict[str, VerifiedObject] = {}
        stack = list(roots.items())
        total_bytes = 0
        while stack:
            if callable(prefetch):
                # Bound metadata only. Bytes are read and verified one object at a
                # time, so a batch of large blobs cannot amplify memory use.
                prefetch(oid for oid, _kind in reversed(stack[-100:]) if oid not in records)
            oid, expected = stack.pop()
            if (
                len(oid) != self.width
                or not set(oid) <= set("0123456789abcdef")
                or oid == "0" * self.width
            ):
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
                loose = reader.get_durable(oid)
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
        encoded = json.dumps(
            {
                "version": 1,
                "object_format": self.object_format,
                "roots": dict(roots),
                "objects": [
                    (oid, record.kind, record.size, record.body_sha256)
                    for oid, record in sorted(records.items())
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return ClosureManifest(
            self.object_format,
            MappingProxyType(dict(roots)),
            MappingProxyType(records),
            hashlib.sha256(encoded).hexdigest(),
            total_bytes,
        )

    def _incremental(self, roots, *, progress=None, on_object=None):
        """Stop at retained typed proofs. No historical blob or ancestor walk."""
        if not roots:
            raise ClosureVerificationError("empty publication closure")
        reader = self.backend.durable_readback()
        records, active, new_objects = {}, set(), []
        total_bytes = 0
        stack = [(oid, kind, False) for oid, kind in roots.items()]
        while stack:
            if progress:
                progress()
            # A per-publication negative cache prevents repeated lookups of
            # the same missing/new object while processing a wide tree.
            self.proofs.prefetch(
                oid for oid, _, exiting in stack[-200:] if not exiting and oid not in records
            )
            oid, expected, exiting = stack.pop()
            if exiting:
                record = records[oid]
                children = [records[child] for child, _ in record.edges]
                if record.kind == "blob":
                    logical, maximum = record.size, record.size
                elif record.kind == "tree":
                    logical = sum(child.logical_bytes for child in children)
                    maximum = max((child.max_blob_bytes for child in children), default=0)
                else:
                    selected = [
                        records[child]
                        for child, kind in record.edges
                        if record.kind == "tag" or kind == "tree"
                    ]
                    logical = selected[0].logical_bytes if selected else 0
                    maximum = selected[0].max_blob_bytes if selected else 0
                if logical > 2**63 - 1:
                    raise ClosureVerificationError("logical tree size exceeds counter range")
                records[oid] = VerifiedObject(
                    record.kind, record.size, record.body_sha256, record.edges, logical, maximum
                )
                active.remove(oid)
                new_objects.append(oid)
                continue
            if (
                len(oid) != self.width
                or set(oid) - set("0123456789abcdef")
                or oid == "0" * self.width
            ):
                raise ClosureVerificationError("invalid closure object id")
            if oid in active:
                raise ClosureVerificationError("cyclic object graph")
            if oid in records:
                if expected is not None and records[oid].kind != expected:
                    raise ClosureVerificationError("closure object type mismatch")
                continue
            if len(records) >= self.max_objects:
                raise ClosureVerificationError("closure object budget exceeded")
            trusted = self.proofs.get(oid)
            if trusted is not None:
                if expected is not None and trusted.kind != expected:
                    raise ClosureVerificationError("closure object type mismatch")
                records[oid] = trusted
                continue
            prefetch = getattr(reader, "prefetch_durable", None)
            if prefetch:
                candidates = [
                    oid,
                    *(
                        child
                        for child, _, done in reversed(stack[-100:])
                        if not done and child not in records and self.proofs.get(child) is None
                    ),
                ]
                prefetch(candidates)
            try:
                loose = reader.get_durable(oid)
                kind, body = decode_object(loose, max_bytes=self.max_bytes - total_bytes)
                if (expected is not None and kind != expected) or hash_object(
                    kind, body, object_format=self.object_format
                ) != oid:
                    raise ValueError("closure object hash/type mismatch")
                edges = tuple(object_edges(kind, body, object_format=self.object_format))
            except Exception as exc:
                raise ClosureVerificationError(f"cannot verify {oid}: {exc}") from exc
            total_bytes += len(body)
            digest = hashlib.sha256(f"{kind} {len(body)}\0".encode() + body).hexdigest()
            records[oid] = VerifiedObject(kind, len(body), digest, edges)
            active.add(oid)
            stack.append((oid, kind, True))
            stack.extend((child, child_kind, False) for child, child_kind in edges)
            if on_object:
                on_object(oid, loose)
        roots = {oid: records[oid].kind for oid in roots}
        encoded = json.dumps(
            {
                "version": 2,
                "object_format": self.object_format,
                "roots": roots,
                "objects": [
                    (oid, record.kind, record.size, record.body_sha256)
                    for oid, record in sorted(records.items())
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return ClosureManifest(
            self.object_format,
            MappingProxyType(roots),
            MappingProxyType(records),
            hashlib.sha256(encoded).hexdigest(),
            total_bytes,
            tuple(new_objects),
            self.proofs.get,
        )
