"""Immutable chunk placements, with legacy-layout read compatibility.

Git OIDs identify uncompressed objects, not a compressor or chunk partition.
Every new physical key therefore identifies its exact bytes as well. A late
PUT from another producer can only replace a key with the same content.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import NoReturn

from src.version_engine.domain.errors import StorageWriteError


def chunk_upload_plan(
    bundle_prefix: str, oid: str, data: bytes, chunk_bytes: int,
) -> tuple[list[tuple[str, bytes, str]], str]:
    if chunk_bytes <= 0 or not data:
        raise ValueError("chunk size and object size must be positive")
    root = f"{bundle_prefix}/chunked/{oid[:2]}/{oid}"
    uploads = []
    chunks = []
    for offset in range(0, len(data), chunk_bytes):
        part = data[offset:offset + chunk_bytes]
        key = f"{root}/part-{hashlib.sha256(part).hexdigest()}"
        uploads.append((key, part, "application/octet-stream"))
        chunks.append({"key": key, "offset_bytes": offset, "size_bytes": len(part)})
    manifest = json.dumps(
        # Keep the existing manifest wire version: old readers already follow
        # explicit keys and ignore additional fields. Only placement changes.
        {"version": 1, "placement": "content-addressed-v1", "object_id": oid,
         "size_bytes": len(data), "chunks": chunks},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    manifest_key = f"{root}/manifest-{hashlib.sha256(manifest).hexdigest()}.json"
    uploads.append((manifest_key, manifest, "application/json"))
    return uploads, manifest_key


def chunk_manifest_root(key: str, oid: str, bundle_prefixes: tuple[str, ...]) -> str:
    """Reject foreign locations before even downloading the manifest."""
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        for prefix in bundle_prefixes:
            root = f"{prefix}/chunked/{oid[:2]}/{oid}"
            if key == root + ".json" or re.fullmatch(
                re.escape(root) + r"/manifest-[0-9a-f]{64}\.json", key,
            ):
                return root
    raise StorageWriteError(f"invalid chunk manifest for {oid}: manifest namespace")


def validate_chunk_manifest(
    raw: bytes, *, key: str, oid: str, size: int, bundle_prefixes: tuple[str, ...],
    require_immutable: bool = False,
) -> tuple[bool, list[dict]]:
    """Validate all locations and byte ranges before issuing any part I/O."""
    def invalid(reason: str) -> NoReturn:
        raise StorageWriteError(f"invalid chunk manifest for {oid}: {reason}")

    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        invalid("JSON")
    if not isinstance(manifest, dict) or manifest.get("object_id") != oid:
        invalid("object identity")
    version = manifest.get("version")
    if type(version) is not int or version != 1:
        invalid("version")
    immutable = manifest.get("placement") == "content-addressed-v1"
    if "placement" in manifest and not immutable:
        invalid("placement")
    if require_immutable and not immutable:
        invalid("native publication requires immutable chunk placements")
    root = chunk_manifest_root(key, oid, bundle_prefixes)
    expected = root + (
        f"/manifest-{hashlib.sha256(raw).hexdigest()}.json" if immutable else ".json"
    )
    if key != expected:
        invalid("manifest namespace or digest")
    if type(manifest.get("size_bytes")) is not int or manifest["size_bytes"] != size or size <= 0:
        invalid("object size")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        invalid("chunk list")
    for chunk in chunks:
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("key"), str)
                or type(chunk.get("offset_bytes")) is not int
                or type(chunk.get("size_bytes")) is not int
                or chunk["offset_bytes"] < 0 or chunk["size_bytes"] <= 0):
            invalid("chunk shape")
    ordered = sorted(chunks, key=lambda part: part["offset_bytes"])
    cursor = 0
    root += "/part-"
    for index, chunk in enumerate(ordered, start=1):
        if chunk["offset_bytes"] != cursor:
            invalid("overlapping or discontinuous chunks")
        if not immutable:
            if chunk["key"] != f"{root}{index:06d}":
                invalid("legacy chunk namespace")
        elif not chunk["key"].startswith(root) or not re.fullmatch(r"[0-9a-f]{64}", chunk["key"][len(root):]):
            invalid("chunk namespace or digest")
        cursor += chunk["size_bytes"]
    if cursor != size:
        invalid("chunk coverage")
    return immutable, ordered
