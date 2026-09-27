"""Inventory legacy Project S3 prefixes before enabling deletion.

This is a self-contained immutable data migration.  Do not import application
modules: the data-migration runner executes it in isolated Python mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from supabase import create_client

SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
LEGACY_PROJECT_KEY = re.compile(
    rf"^users/(?P<principal>{SEGMENT})/" rf"(?:etl_artifacts|processed|raw)/(?P<project>{SEGMENT})/"
)


@dataclass(frozen=True)
class InventoryProof:
    object_count: int
    multipart_count: int
    digest: str


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def batch_size() -> int:
    value = int(os.environ.get("DATA_MIGRATION_BATCH_SIZE", "1000"))
    if not 1 <= value <= 1000:
        raise RuntimeError("DATA_MIGRATION_BATCH_SIZE must be between 1 and 1000")
    return value


def parse_key(key: str) -> tuple[str, str] | None:
    matched = LEGACY_PROJECT_KEY.match(key)
    if matched is None:
        return None
    return matched.group("project"), matched.group("principal")


def rpc_object(data: Any) -> dict[str, Any]:
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError("inventory RPC returned an invalid response")
    return data


class Repository:
    def __init__(self) -> None:
        self.client = create_client(required("SUPABASE_URL"), required("SUPABASE_KEY"))

    def status(self) -> dict[str, Any]:
        return rpc_object(self.client.rpc("project_storage_inventory_status", {}).execute().data)

    def is_complete(self) -> bool:
        return bool(self.status().get("inventory_complete"))

    def checkpoint(self) -> dict[str, Any]:
        checkpoint = self.status().get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise RuntimeError("Project storage inventory checkpoint is invalid")
        return checkpoint

    def record_batch(
        self,
        batch_key: str,
        principals: set[tuple[str, str]],
        checkpoint: dict[str, Any],
        object_count: int,
        multipart_count: int,
    ) -> None:
        outcome = rpc_object(
            self.client.rpc(
                "record_project_storage_inventory_batch",
                {
                    "p_batch_key": batch_key,
                    "p_principals": [
                        {"project_id": project, "principal": principal}
                        for project, principal in sorted(principals)
                    ],
                    "p_checkpoint": checkpoint,
                    "p_observed_object_count": object_count,
                    "p_observed_multipart_count": multipart_count,
                },
            )
            .execute()
            .data
        )
        if outcome.get("outcome") not in {"recorded", "replayed"}:
            raise RuntimeError(f"inventory batch was rejected: {outcome}")

    def project_ids(self) -> set[str]:
        project_ids: set[str] = set()
        offset = 0
        while True:
            rows = (
                self.client.table("projects")
                .select("id")
                .range(offset, offset + 999)
                .execute()
                .data
                or []
            )
            project_ids.update(str(row["id"]) for row in rows if row.get("id"))
            if len(rows) < 1000:
                return project_ids
            offset += 1000

    def pending_orphans(self) -> list[tuple[str, str]]:
        rows = self.status().get("pending_orphans")
        if not isinstance(rows, list):
            raise RuntimeError("Project storage inventory orphan state is invalid")
        return sorted(
            (str(row["project_id"]), str(row["principal"]))
            for row in rows
            if isinstance(row, dict) and row.get("project_id") and row.get("principal")
        )

    def mark_orphan_cleaned(self, project_id: str, principal: str) -> None:
        response = self.client.rpc(
            "mark_project_storage_orphan_cleaned",
            {"p_project_id": project_id, "p_principal": principal},
        ).execute()
        if not bool(response.data):
            raise RuntimeError("orphan cleanup was not acknowledged")

    def finalize(self, proof: InventoryProof) -> None:
        outcome = rpc_object(
            self.client.rpc(
                "finalize_project_storage_inventory_scan",
                {
                    "p_observed_object_count": proof.object_count,
                    "p_observed_multipart_count": proof.multipart_count,
                    "p_inventory_digest": proof.digest,
                },
            )
            .execute()
            .data
        )
        if outcome.get("outcome") not in {"finalized", "already_complete"}:
            raise RuntimeError(f"inventory finalization was rejected: {outcome}")

    def verify(self, proof: InventoryProof) -> None:
        outcome = rpc_object(
            self.client.rpc(
                "verify_project_storage_inventory",
                {
                    "p_observed_object_count": proof.object_count,
                    "p_observed_multipart_count": proof.multipart_count,
                    "p_inventory_digest": proof.digest,
                },
            )
            .execute()
            .data
        )
        if outcome.get("outcome") != "verified":
            raise RuntimeError(f"inventory verification was rejected: {outcome}")

    def complete(self) -> None:
        outcome = rpc_object(
            self.client.rpc("complete_project_storage_inventory", {}).execute().data
        )
        if outcome.get("outcome") not in {"completed", "replayed"}:
            raise RuntimeError(f"inventory completion was rejected: {outcome}")


PageCallback = Callable[[str, list[tuple[str, str]], set[tuple[str, str]], dict[str, Any]], None]


def observe(
    s3: Any,
    bucket: str,
    size: int,
    *,
    checkpoint: dict[str, Any] | None = None,
    on_page: PageCallback | None = None,
) -> InventoryProof:
    checkpoint = checkpoint or {}
    phase = str(checkpoint.get("phase") or "objects")
    if phase not in {"objects", "multipart", "complete"}:
        raise RuntimeError("inventory checkpoint has an invalid phase")
    digest = hashlib.sha256()
    object_count = multipart_count = 0
    continuation = checkpoint.get("continuation_token") if phase == "objects" else None
    if continuation is not None and not isinstance(continuation, str):
        raise RuntimeError("inventory object checkpoint is invalid")

    objects_complete = phase in {"multipart", "complete"} or bool(
        checkpoint.get("objects_complete")
    )
    while not objects_complete:
        response = s3.list_objects_v2(
            Bucket=bucket,
            Prefix="users/",
            MaxKeys=size,
            **({"ContinuationToken": continuation} if continuation else {}),
        )
        keys = [str(item["Key"]) for item in response.get("Contents", [])]
        for key in keys:
            digest.update(b"object\0")
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
        object_count += len(keys)
        next_token = response.get("NextContinuationToken")
        truncated = bool(response.get("IsTruncated"))
        if truncated and not next_token:
            raise RuntimeError("S3 object inventory omitted its continuation token")
        if on_page is not None:
            on_page(
                "objects",
                [(key, "") for key in keys],
                {parsed for key in keys if (parsed := parse_key(key)) is not None},
                {
                    "phase": "objects" if truncated else "multipart",
                    "objects_complete": not truncated,
                    "continuation_token": next_token,
                },
            )
        if not truncated:
            objects_complete = True
        continuation = str(next_token) if next_token else None

    key_marker = checkpoint.get("key_marker") if phase == "multipart" else None
    upload_marker = checkpoint.get("upload_id_marker") if phase == "multipart" else None
    if key_marker is not None and not isinstance(key_marker, str):
        raise RuntimeError("inventory multipart checkpoint is invalid")
    if upload_marker is not None and not isinstance(upload_marker, str):
        raise RuntimeError("inventory multipart checkpoint is invalid")
    multipart_complete = phase == "complete" or bool(checkpoint.get("multipart_complete"))
    while not multipart_complete:
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": "users/",
            "MaxUploads": size,
        }
        if key_marker is not None:
            params.update({"KeyMarker": key_marker, "UploadIdMarker": upload_marker or ""})
        response = s3.list_multipart_uploads(**params)
        uploads = [
            (str(item["Key"]), str(item["UploadId"])) for item in response.get("Uploads", [])
        ]
        for key, upload_id in uploads:
            digest.update(b"multipart\0")
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(upload_id.encode("utf-8"))
            digest.update(b"\0")
        multipart_count += len(uploads)
        next_key, next_upload = (
            response.get("NextKeyMarker"),
            response.get("NextUploadIdMarker"),
        )
        truncated = bool(response.get("IsTruncated"))
        if truncated and (not next_key or not next_upload):
            raise RuntimeError("S3 multipart inventory requires both pagination markers")
        if on_page is not None:
            on_page(
                "multipart",
                uploads,
                {parsed for key, _ in uploads if (parsed := parse_key(key)) is not None},
                {
                    "phase": "multipart" if truncated else "complete",
                    "objects_complete": True,
                    "multipart_complete": not truncated,
                    "key_marker": next_key,
                    "upload_id_marker": next_upload,
                },
            )
        if not truncated:
            multipart_complete = True
        key_marker = str(next_key) if next_key else None
        upload_marker = str(next_upload) if next_upload else None
    return InventoryProof(object_count, multipart_count, digest.hexdigest())


def purge_prefix(s3: Any, bucket: str, prefix: str) -> None:
    while True:
        keys = [
            str(item["Key"])
            for item in s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1000).get(
                "Contents", []
            )
        ]
        if not keys:
            break
        errors = s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
        ).get("Errors", [])
        if errors:
            raise RuntimeError("S3 rejected an orphan object deletion")
    while True:
        uploads = s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix, MaxUploads=1000).get(
            "Uploads", []
        )
        if not uploads:
            break
        for upload in uploads:
            s3.abort_multipart_upload(
                Bucket=bucket, Key=str(upload["Key"]), UploadId=str(upload["UploadId"])
            )


def clean_orphans(s3: Any, bucket: str, repository: Repository) -> None:
    for project_id, principal in repository.pending_orphans():
        if not re.fullmatch(SEGMENT, project_id) or not re.fullmatch(SEGMENT, principal):
            raise RuntimeError("inventory contains an invalid orphan prefix")
        prefixes = [
            f"users/{principal}/{namespace}/{project_id}/"
            for namespace in ("etl_artifacts", "processed", "raw")
        ]
        for prefix in prefixes:
            purge_prefix(s3, bucket, prefix)
            if s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get(
                "Contents"
            ) or s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix, MaxUploads=1).get(
                "Uploads"
            ):
                raise RuntimeError(f"orphan Project prefix is not empty: {prefix}")
        repository.mark_orphan_cleaned(project_id, principal)


def main(apply: bool) -> None:
    repository = Repository()
    if repository.is_complete():
        print("[inventory] already complete")
        return
    bucket = required("S3_BUCKET_NAME")
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
    s3 = boto3.client(
        "s3",
        aws_access_key_id=required("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=required("S3_SECRET_ACCESS_KEY"),
        region_name=required("S3_REGION"),
        endpoint_url=endpoint or None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    size = batch_size()
    discovered: set[tuple[str, str]] = set()

    def record(
        kind: str,
        entries: list[tuple[str, str]],
        principals: set[tuple[str, str]],
        checkpoint: dict[str, Any],
    ) -> None:
        discovered.update(principals)
        if apply:
            canonical = json.dumps(
                {"kind": kind, "entries": entries},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            repository.record_batch(
                hashlib.sha256(canonical).hexdigest(),
                principals,
                {"kind": kind, **checkpoint},
                len(entries) if kind == "objects" else 0,
                len(entries) if kind == "multipart" else 0,
            )

    discovery = observe(
        s3,
        bucket,
        size,
        checkpoint=repository.checkpoint() if apply else None,
        on_page=record,
    )
    live_project_ids = repository.project_ids()
    unknown = {
        (project, principal) for project, principal in discovered if project not in live_project_ids
    }
    if unknown and not apply:
        raise RuntimeError(
            "historical storage contains unknown Project prefixes; re-run with --apply"
        )
    if not apply:
        print(
            f"[inventory] dry-run objects={discovery.object_count} multipart={discovery.multipart_count}"
        )
        return
    clean_orphans(s3, bucket, repository)
    first = observe(s3, bucket, size)
    repository.finalize(first)
    second = observe(s3, bucket, size)
    if second != first:
        raise RuntimeError("Project storage changed between inventory passes; rerun the migration")
    repository.verify(second)
    repository.complete()
    print(
        f"[inventory] completed objects={first.object_count} multipart={first.multipart_count} digest={first.digest}"
    )


if __name__ == "__main__":
    try:
        main(apply="--apply" in sys.argv[1:])
    except Exception as error:
        print(f"[inventory] failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
