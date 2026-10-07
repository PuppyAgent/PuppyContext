#!/usr/bin/env python3
"""Apply the public schema with native Supabase history, never an initdb snapshot.

Existing pre-B1 databases must complete the documented phased archive upgrade
first. Admission refuses to stamp an incomplete or divergent database.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.infra.data_migrations.baseline_adoption import adoption_sql
from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.runner import DataMigrationRunner
from database_history import resolve_release


def migrate_data(root: Path, db: PsqlClient) -> None:
    selection = resolve_release(root, "standalone")
    if selection["execution_mode"] != "ci":
        raise RuntimeError(
            "Standalone data migration requires explicit operator completion"
        )
    runner = DataMigrationRunner(
        DataMigrationCatalog(root),
        db,
        environment={},
        source_sha="standalone-release",
    )
    for migration_id in (selection["repair_migration_id"], selection["migration_id"]):
        if migration_id:
            runner.run(migration_id)
            # A receipt does not prove the current data still satisfies the contract.
            runner.verify(migration_id)


def push_schema(root: Path, db: PsqlClient, target: str, password: str) -> None:
    """Resume declared SQL data gates before retrying dependent Contract DDL."""
    catalog = DataMigrationCatalog(root)
    approved = {}
    for migration in (root / "supabase/migrations").glob("*.sql"):
        body = migration.read_text()
        prerequisite = re.search(r"^-- requires-data-migration: (\w+)$", body, re.M)
        checksum = re.search(r"^-- data-migration-checksum: ([0-9a-f]{64})$", body, re.M)
        if prerequisite and checksum:
            approved[prerequisite[1]] = checksum[1]
    attempted = set()
    runner = DataMigrationRunner(catalog, db, environment={}, source_sha="standalone-release")
    while True:
        result = subprocess.run(
            ["supabase", "db", "push", "--db-url", target, "--yes", "--workdir", str(root)],
            env={**os.environ, "PGPASSWORD": password, "PGSSLMODE": "disable"},
            check=False, timeout=600, text=True, capture_output=True,
        )
        if result.returncode == 0:
            return
        gate = re.search(r"DATA_MIGRATION_REQUIRED:([0-9A-Za-z_]+)", result.stdout + result.stderr)
        if not gate or gate[1] not in approved or gate[1] in attempted:
            raise RuntimeError("Schema deployment failed outside an approved data gate")
        artifact = catalog.get(gate[1])
        if artifact.checksum != approved[gate[1]] or artifact.manifest.kind != "sql":
            raise RuntimeError("Data gate requires separately completed operator migration")
        attempted.add(gate[1])
        runner.run(gate[1])
        runner.verify(gate[1])


def migrate() -> None:
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("PGHOST", "db")
    port = int(os.environ.get("PGPORT", "5432"))
    # The database password is passed through the environment, not argv/logs.
    # This task connects only inside the private Compose network. Its local
    # PostgreSQL service does not terminate TLS; hosted release URLs are owned
    # by the separate deployment workflow and retain their TLS requirements.
    target = f"postgresql://postgres@{host}:{port}/postgres?sslmode=disable"
    db = PsqlClient(
        f"postgresql://postgres:{quote(password, safe='')}@{host}:{port}/postgres"
    )
    with db.advisory_lock("puppyone-self-hosted-release"):
        db.command(
            ["-q", "-v", "ON_ERROR_STOP=1"],
            input_text=adoption_sql(ROOT, apply=True),
            timeout=180,
        )
        push_schema(ROOT, db, target, password)
        expected = {
            p.name.split("_", 1)[0]
            for p in (ROOT / "supabase/migrations").glob("*.sql")
        }
        if db.applied_schema_versions() != expected:
            raise RuntimeError("Migration history does not match this release")
        # Recheck B1 admission (including its fingerprint when still at B1).
        db.command(
            ["-q", "-v", "ON_ERROR_STOP=1"],
            input_text=adoption_sql(ROOT, apply=False),
            timeout=180,
        )
        migrate_data(ROOT, db)
        # Install the explicit standalone policy with the database owner, never
        # with API credentials or fabricated hosted entitlement records.
        entitlement_mode = os.environ.get("ENTITLEMENTS_MODE", "disabled")
        if entitlement_mode not in {"disabled", "db"}:
            raise RuntimeError(
                "Standalone native repository policy requires disabled or db mode"
            )
        db.scalar(
            "SELECT public.configure_repository_entitlement_source('"
            + entitlement_mode
            + "');"
        )
        db.scalar("NOTIFY pgrst, 'reload schema';")
    print(
        "Public schema is at the checked-out release; no demo data or private billing schema installed."
    )


if __name__ == "__main__":
    try:
        migrate()
    except Exception as error:  # noqa: BLE001 -- sanitized CLI boundary
        # Database diagnostics may contain row data. The operator can inspect
        # local database logs; do not print connection strings or exception text.
        raise SystemExit(
            f"Self-hosted migration failed ({type(error).__name__}); application startup blocked."
        ) from None
