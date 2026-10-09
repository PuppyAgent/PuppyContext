"""Apply bounded schema phases using the official Supabase migration history."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from ..baseline_adoption import adoption_sql
from ..database import PsqlClient


class Schema:
    def __init__(self, root: Path, database: PsqlClient):
        self.root = root
        self.db = database
        self.files = {p.name[:14]: p for p in sorted((root / "supabase/migrations").glob("*.sql"))}

    def pending(self, through: str) -> list[Path]:
        end = max(self.files) if through == "latest" else through
        applied = self.applied()
        unknown = applied - self.files.keys()
        if unknown:
            raise ValueError("Target history is not supported by this release")
        return [
            path
            for version, path in self.files.items()
            if version <= end and version not in applied
        ]

    def adopt(self) -> None:
        self.db.command(
            ["-q", "-v", "ON_ERROR_STOP=1"],
            input_text=adoption_sql(self.root, apply=True),
            timeout=180,
        )

    def applied(self) -> set[str]:
        if (
            self.db.scalar("SELECT to_regclass('supabase_migrations.schema_migrations') IS NULL")
            == "t"
        ):
            return set()
        return self.db.applied_schema_versions()

    def apply(self, through: str) -> None:
        pending = self.pending(through)
        if not pending:
            return
        # Keep already-applied later timestamps in the temporary history, while
        # withholding every *unapplied* migration beyond this phase boundary.
        applied = self.applied()
        selected = applied | {p.name[:14] for p in pending}
        environment = {**os.environ, **self.db.environment}
        host = environment["PGHOST"]
        if ":" in host:
            host = f"[{host}]"
        target = (
            f"postgresql://{quote(environment.get('PGUSER', 'postgres'), safe='')}@{host}:"
            f"{environment['PGPORT']}/{quote(environment['PGDATABASE'], safe='')}"
            f"?sslmode={environment.get('PGSSLMODE', 'require')}"
        )
        with tempfile.TemporaryDirectory(prefix="puppyone-schema-phase-") as temporary:
            staged = Path(temporary) / "supabase"
            (staged / "migrations").mkdir(parents=True)
            shutil.copyfile(self.root / "supabase/config.toml", staged / "config.toml")
            for version in sorted(selected):
                shutil.copyfile(
                    self.files[version], staged / "migrations" / self.files[version].name
                )
            result = subprocess.run(
                [
                    "supabase",
                    "db",
                    "push",
                    "--db-url",
                    target,
                    "--include-all",
                    "--yes",
                    "--workdir",
                    temporary,
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=1800,
                check=False,
            )
            if result.returncode:
                # SQL errors can contain customer rows and connection data.
                if directory := environment.get("RELEASE_PRIVATE_ARTIFACTS"):
                    diagnostic = Path(directory) / "schema-error.log"
                    diagnostic.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    diagnostic.touch(mode=0o600, exist_ok=True)
                    diagnostic.write_text(result.stdout + result.stderr)
                raise RuntimeError(f"Schema phase failed through {through}")
        if self.pending(through):
            raise RuntimeError("Schema phase did not apply its complete history")

    def verify(self) -> None:
        if self.db.applied_schema_versions() != set(self.files):
            raise RuntimeError("Candidate migration history differs from target")
        for name in (
            "schema_contracts.inc",
            "data_api_containment.inc",
            "cloud_agent_contracts.inc",
        ):
            # These existing contracts own BEGIN/ROLLBACK themselves. The schema
            # probe intentionally exercises writes and rolls them back; the data
            # artifact verifier's outer READ ONLY transaction would reject it.
            self.db.command(
                [
                    "-q",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "SET statement_timeout='120s'; SET lock_timeout='5s';",
                    "-f",
                    str(self.root / "supabase/tests/_support" / name),
                ],
                timeout=120,
            )
        self.verify_drift()

    def verify_drift(self) -> None:
        environment = {**os.environ, **self.db.environment}
        host = environment["PGHOST"]
        if ":" in host:
            host = f"[{host}]"
        target = (
            f"postgresql://{quote(environment.get('PGUSER', 'postgres'), safe='')}@{host}:"
            f"{environment['PGPORT']}/{quote(environment['PGDATABASE'], safe='')}"
            f"?sslmode={environment.get('PGSSLMODE', 'require')}"
        )
        # The CLI needs a disposable shadow database. A unique project ID avoids
        # collision with any developer's unrelated Supabase stack.
        with tempfile.TemporaryDirectory(prefix="puppyone-release-drift-") as temporary:
            staged = Path(temporary) / "supabase"
            staged.mkdir()
            config = (self.root / "supabase/config.toml").read_text()
            config = re.sub(
                r"^project_id = .*$",
                'project_id = "release-drift-' + secrets.token_hex(8) + '"',
                config,
                flags=re.M,
            )
            (staged / "config.toml").write_text(config)
            shutil.copytree(self.root / "supabase/migrations", staged / "migrations")
            result = subprocess.run(
                [
                    "supabase",
                    "db",
                    "diff",
                    "--db-url",
                    target,
                    "--schema",
                    "public",
                    "--workdir",
                    temporary,
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=900,
            )
            output = result.stdout + result.stderr
            if result.returncode or (output.strip() and "No schema changes found" not in output):
                if directory := environment.get("RELEASE_PRIVATE_ARTIFACTS"):
                    diagnostic = Path(directory) / "schema-drift.log"
                    diagnostic.touch(mode=0o600, exist_ok=True)
                    diagnostic.write_text(output)
                raise RuntimeError("Candidate public schema drift verification failed")
