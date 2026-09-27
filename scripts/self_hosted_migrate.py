#!/usr/bin/env python3
"""Apply the public schema with native Supabase history, never an initdb snapshot.

Existing pre-B1 databases must complete the documented phased archive upgrade
first. Admission refuses to stamp an incomplete or divergent database.
"""

from __future__ import annotations

import os
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
        subprocess.run(
            [
                "supabase",
                "db",
                "push",
                "--db-url",
                target,
                "--yes",
                "--workdir",
                str(ROOT),
            ],
            # CLI 2.107 reconstructs --db-url and drops its sslmode parameter
            # (internal/utils/connect.go: ToPostgresURL). pgx still honors the
            # process-scoped environment; do not use --debug as a TLS workaround.
            env={
                **os.environ,
                # --db-url uses pgconn directly; SUPABASE_DB_PASSWORD is only
                # consumed by the CLI's linked-project connection path.
                "PGPASSWORD": password,
                "PGSSLMODE": "disable",
            },
            check=True,
            timeout=600,
        )
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
