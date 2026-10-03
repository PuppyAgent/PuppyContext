#!/usr/bin/env python3
"""Owner-operated, redacted ISSUE-049 inventory and reviewed decisions.

No automatic classification. Default target is loopback only. Remote use requires
explicit operator authorization and a provable Supabase project/API/DB pairing.
This tool records decisions; the immutable data runner owns copying and receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from src.infra.data_migrations.database import PsqlClient

INVENTORY = """
BEGIN READ ONLY;
SET LOCAL statement_timeout='30s';
SELECT coalesce(jsonb_agg(jsonb_build_object(
  'legacy_id', b.id, 'project_id', b.project_id,
  'snapshot_sha256', public.entrypoint_source_snapshot(b.id),
  'facts', jsonb_build_object('provider', b.provider, 'trigger_type', b.trigger_type,
    'status', b.status, 'has_database_config', b.config ? 'db_config',
    'has_oauth', b.oauth_connection_id IS NOT NULL,
    'run_count', (SELECT count(*) FROM public.sync_runs r WHERE r.connection_id=b.id)),
  'disposition', NULL, 'import_database_source_id', NULL,
  'binding_read_only_reason', NULL, 'evidence_ref', NULL, 'approved_by', NULL
) ORDER BY b.id),'[]') FROM public.connections b;
ROLLBACK;
"""
FIELDS = {
    "legacy_id",
    "project_id",
    "snapshot_sha256",
    "disposition",
    "import_database_source_id",
    "binding_read_only_reason",
    "evidence_ref",
    "approved_by",
}


def reviewed_rows(document: dict) -> list[dict]:
    if (
        not isinstance(document, dict)
        or set(document) != {"format_version", "rows"}
        or type(document["format_version"]) is not int
        or document["format_version"] != 1
    ):
        raise ValueError("Expected format_version=1 and rows")
    if not isinstance(document["rows"], list):
        raise TypeError("Rows must be an array")
    rows, ids, source_ids = [], set(), set()
    for row in document["rows"]:
        if (
            not isinstance(row, dict)
            or not FIELDS <= row.keys()
            or row.keys() - FIELDS - {"facts"}
        ):
            raise ValueError("Unknown or missing decision fields")
        item = {key: row[key] for key in FIELDS}
        for key in ("legacy_id", "project_id", "evidence_ref", "approved_by"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(
                    "Each decision requires explicit identity, reviewer and evidence"
                )
        if not isinstance(item["snapshot_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", item["snapshot_sha256"]
        ):
            raise ValueError("Invalid snapshot digest")
        if item["legacy_id"] in ids:
            raise ValueError("Duplicate legacy identity")
        ids.add(item["legacy_id"])
        if item["disposition"] not in {"synchronize", "import", "both"}:
            raise ValueError(
                "Every row requires a reviewed disposition; none is inferred"
            )
        source = item["import_database_source_id"]
        if item["disposition"] == "synchronize":
            if source is not None:
                raise ValueError(
                    "Synchronize-only decision cannot create an Import source"
                )
        else:
            if (
                not isinstance(source, str)
                or not source.strip()
                or source in source_ids
            ):
                raise ValueError("Missing or duplicate Import source identity")
            if item["disposition"] == "import" and source != item["legacy_id"]:
                raise ValueError("Import-only source ID must be preserved")
            source_ids.add(source)
        reason = item["binding_read_only_reason"]
        if reason is not None and (
            item["disposition"] not in {"synchronize", "both"}
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError(
                "Read-only retention must explicitly retain a Synchronize binding"
            )
        rows.append(item)
    return rows


def approve(db: PsqlClient, rows: list[dict]) -> None:
    # Do not erase previous reviews implicitly. An exact re-import is idempotent;
    # changing a review requires an explicit owner correction and a new inventory.
    payload = json.dumps(rows, separators=(",", ":"))
    sql = """
BEGIN;
SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s';
LOCK TABLE public.connections, public.sync_runs, public.entrypoint_source_decisions IN SHARE ROW EXCLUSIVE MODE;
CREATE TEMP TABLE reviewed_decisions ON COMMIT DROP AS
SELECT * FROM jsonb_populate_recordset(NULL::public.entrypoint_source_decisions, :'decisions'::jsonb);
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM reviewed_decisions d LEFT JOIN public.connections b ON b.id=d.legacy_id
    WHERE b.id IS NULL OR d.project_id<>b.project_id OR d.snapshot_sha256 IS DISTINCT FROM public.entrypoint_source_snapshot(b.id)) THEN
    RAISE EXCEPTION 'ENTRYPOINT_CLASSIFICATION_REQUIRED';
  END IF;
  IF EXISTS (SELECT 1 FROM reviewed_decisions d JOIN public.entrypoint_source_decisions old USING (legacy_id)
    WHERE (to_jsonb(d)-'approved_at') IS DISTINCT FROM (to_jsonb(old)-'approved_at')) THEN
    RAISE EXCEPTION 'ENTRYPOINT_REVIEW_CONFLICT';
  END IF;
END $$;
INSERT INTO public.entrypoint_source_decisions
SELECT legacy_id,project_id,snapshot_sha256,disposition,import_database_source_id,
  binding_read_only_reason,evidence_ref,approved_by,now() FROM reviewed_decisions
ON CONFLICT (legacy_id) DO NOTHING;
COMMIT;
"""
    db.scalar(sql, variables={"decisions": payload})


def freeze(
    db: PsqlClient, evidence_bytes: bytes, *, restore_point_ref: str, approved_by: str
) -> None:
    evidence = json.loads(evidence_bytes)
    if (
        not isinstance(evidence, dict)
        or not all(
            isinstance(value, str) and value.strip()
            for value in (restore_point_ref, approved_by, evidence.get("environment"))
        )
        or not all(
            evidence.get(key) is True
            for key in (
                "producer_stop_verified",
                "queue_drain_verified",
                "old_consumers_exited",
            )
        )
        or not isinstance(evidence.get("records"), list)
        or not evidence["records"]
        or not all(
            isinstance(record, dict) and record for record in evidence["records"]
        )
    ):
        raise ValueError(
            "Freeze requires reviewed process/queue/consumer records, environment and restore point"
        )
    db.scalar(
        """
BEGIN;
SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s';
-- Serialize with writes already in flight, not just future trigger invocations.
LOCK TABLE public.connections, public.sync_runs, public.github_sync_bindings, public.github_sync_log,
  public.import_database_sources, public.access_tools, public.search_index_tasks IN SHARE ROW EXCLUSIVE MODE;
UPDATE public.entrypoint_cutover_control SET writes_frozen=true, migration_token=gen_random_uuid(),
  consumer_evidence_sha256=:'digest', restore_point_ref=:'restore', approved_by=:'actor', frozen_at=now()
WHERE singleton;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.entrypoint_cutover_control WHERE singleton AND writes_frozen) THEN
    RAISE EXCEPTION 'ENTRYPOINT_CONTROL_MISSING';
  END IF;
END $$;
COMMIT;
""",
        variables={
            "digest": hashlib.sha256(evidence_bytes).hexdigest(),
            "restore": restore_point_ref,
            "actor": approved_by,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "approve", "freeze"])
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="New inventory output, reviewed decisions input or consumer evidence file",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--restore-point-ref")
    parser.add_argument("--approved-by")
    args = parser.parse_args()
    url = os.environ.get("DATA_MIGRATION_DATABASE_URL", "")
    host = urlsplit(url).hostname
    if host not in {"localhost", "127.0.0.1", "::1"} and not args.allow_remote:
        raise ValueError(
            "Refusing a non-loopback database; no remote operation is implicitly authorized"
        )
    db = PsqlClient(url)
    if host not in {"localhost", "127.0.0.1", "::1"}:
        db.assert_supabase_target(
            project_ref=os.environ.get("SUPABASE_PROJECT_ID", ""),
            api_url=os.environ.get("SUPABASE_URL", ""),
        )
    if args.action == "inventory":
        data = {"format_version": 1, "rows": json.loads(db.scalar(INVENTORY))}
        fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2)
        print(
            f"Wrote {len(data['rows'])} redacted rows; all dispositions remain unassigned"
        )
        return
    if not args.apply:
        raise ValueError("Writes require --apply after review")
    if args.action == "approve":
        rows = reviewed_rows(json.loads(args.file.read_text()))
        approve(db, rows)
        print(
            f"Recorded {len(rows)} explicit decisions; no source copied or migration receipt written"
        )
        return
    freeze(
        db,
        args.file.read_bytes(),
        restore_point_ref=args.restore_point_ref,
        approved_by=args.approved_by,
    )
    print(
        "Application writes frozen. Recorded evidence is an operator attestation, not an independent process scan."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- redact all database/parse diagnostics at the CLI boundary
        # Database diagnostics may include encrypted config or other row data.
        raise SystemExit(
            f"Entrypoint decision operation failed ({type(error).__name__}); no success attestation. Review the protected local inputs/logs."
        ) from None
