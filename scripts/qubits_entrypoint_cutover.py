#!/usr/bin/env python3
"""Protected Qubits adapter for the existing owner-operated cutover commands.

Inspection emits only hashes, counts and backup metadata. Preparation consumes
reviewed staging secrets; it never infers classifications or stops services.
The ordinary portable data runner still owns migration copying and receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import httpx

from entrypoint_source_decisions import INVENTORY, approve, freeze, reviewed_rows
from src.infra.data_migrations.database import PsqlClient


def verified_pitr_point(backups: dict, ref: str, point: str, evidence: dict) -> None:
    prefix = f"supabase-pitr:{ref}:"
    if not point.startswith(prefix) or not point[len(prefix):].isdigit():
        raise ValueError("CUTOVER_RESTORE_POINT_REQUIRED")
    timestamp = int(point[len(prefix):])
    window = backups.get("physical_backup_data", {})
    if backups.get("pitr_enabled") is not True or not (
        window.get("earliest_physical_backup_date_unix", float("inf"))
        <= timestamp <= window.get("latest_physical_backup_date_unix", 0)
    ):
        raise ValueError("CUTOVER_RESTORE_POINT_UNAVAILABLE")
    stopped = datetime.fromisoformat(evidence["writers_stopped_at"].replace("Z", "+00:00"))
    if stopped.tzinfo is None or timestamp < stopped.timestamp():
        raise ValueError("CUTOVER_RESTORE_POINT_PRECEDES_STOP")
    if evidence.get("environment") != "qubits":
        raise ValueError("CUTOVER_WRONG_EVIDENCE_ENVIRONMENT")


def verified_logical_backup(ref: str, point: str, evidence: dict, backup: dict) -> None:
    """Validate a protected operator attestation, not a cloud-provider backup.

    The operator retains the actual archive and proves restoration separately.
    This adapter cannot attest file availability from a digest alone.
    """
    if evidence.get("environment") != "qubits" or backup.get("project_ref") != ref:
        raise ValueError("CUTOVER_WRONG_EVIDENCE_ENVIRONMENT")
    digest = backup.get("sha256", "")
    if (
        type(backup.get("format_version")) is not int
        or backup["format_version"] != 1
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or point != f"operator-logical:{ref}:sha256:{digest}"
        or type(backup.get("bytes")) is not int
        or backup["bytes"] <= 0
    ):
        raise ValueError("CUTOVER_LOGICAL_BACKUP_REQUIRED")
    for field in ("retained_at", "restore_procedure", "verification_record", "verified_by"):
        if not isinstance(backup.get(field), str) or not backup[field].strip():
            raise ValueError("CUTOVER_LOGICAL_BACKUP_EVIDENCE_REQUIRED")
    if backup.get("restore_verified") is not True:
        raise ValueError("CUTOVER_LOGICAL_RESTORE_UNVERIFIED")
    timestamps = []
    for document, field in ((evidence, "writers_stopped_at"), (backup, "created_at"), (backup, "verified_at")):
        try:
            value = datetime.fromisoformat(document[field].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("CUTOVER_INVALID_BACKUP_TIME") from None
        if value.tzinfo is None:
            raise ValueError("CUTOVER_INVALID_BACKUP_TIME")
        timestamps.append(value)
    stopped, created, verified = timestamps
    if not stopped <= created <= verified <= datetime.now(timezone.utc):
        raise ValueError("CUTOVER_BACKUP_TIME_ORDER_INVALID")


def main() -> None:
    if (
        os.environ.get("GITHUB_REF") != "refs/heads/qubits"
        or os.environ.get("TARGET_ENVIRONMENT") != "staging"
    ):
        raise ValueError("CUTOVER_WRONG_ENVIRONMENT")
    ref = os.environ["SUPABASE_PROJECT_ID"]
    if not re.fullmatch(r"[a-z]{20}", ref):
        raise ValueError("CUTOVER_INVALID_PROJECT")
    db = PsqlClient(os.environ["DATA_MIGRATION_DATABASE_URL"])
    db.assert_supabase_target(project_ref=ref, api_url=os.environ["SUPABASE_URL"])
    response = httpx.get(
        f"https://api.supabase.com/v1/projects/{ref}/database/backups",
        headers={"Authorization": "Bearer " + os.environ["SUPABASE_ACCESS_TOKEN"]},
        timeout=30,
    )
    response.raise_for_status()
    backups = response.json()
    operation = os.environ.get("CUTOVER_OPERATION", "inspect")
    if operation == "inspect":
        completed = db.scalar("SELECT to_regclass('public.synchronize_bindings') IS NOT NULL") == "t"
        rows = [] if completed else json.loads(db.scalar(INVENTORY))
        print(json.dumps({
            "cutover_completed": completed,
            "row_count": len(rows),
            "rows": [{
                "identity_sha256": hashlib.sha256(row["legacy_id"].encode()).hexdigest(),
                "snapshot_sha256": row["snapshot_sha256"],
                "facts": row["facts"],
            } for row in rows],
            "pitr_enabled": backups.get("pitr_enabled"),
            "physical_backup_data": backups.get("physical_backup_data"),
            "backups": [{k: b.get(k) for k in ("id", "status", "inserted_at")}
                        for b in backups.get("backups", [])],
        }, sort_keys=True))
        return
    if operation != "prepare" or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ValueError("CUTOVER_EXPLICIT_PREPARATION_REQUIRED")
    rows = reviewed_rows(json.loads(os.environ["ENTRYPOINT_CUTOVER_DECISIONS"]))
    evidence_bytes = os.environ["ENTRYPOINT_CUTOVER_EVIDENCE"].encode()
    evidence = json.loads(evidence_bytes)
    point = os.environ["ENTRYPOINT_CUTOVER_RESTORE_POINT"]
    if point.startswith("operator-logical:"):
        verified_logical_backup(
            ref, point, evidence, json.loads(os.environ["ENTRYPOINT_CUTOVER_LOGICAL_BACKUP"])
        )
    else:
        verified_pitr_point(backups, ref, point, evidence)
    # These existing commands verify row fingerprints and explicit process,
    # queue and consumer evidence. No receipt or successful migration is faked.
    approve(db, rows)
    freeze(db, evidence_bytes, restore_point_ref=point, approved_by=os.environ["GITHUB_ACTOR"])
    print("Reviewed cutover preparation recorded; run the normal data migration next.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print an SQL exception, input document or authenticated request.
        code = re.search(r"(?:CUTOVER|ENTRYPOINT)_[A-Z_]+", str(error))
        raise SystemExit(code.group(0) if code else f"Cutover operation failed ({type(error).__name__})") from None
