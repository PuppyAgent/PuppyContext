"""Migration planning, execution, verification, and receipt publication."""

from __future__ import annotations

import os
import subprocess
import sys

from .catalog import DataMigrationArtifact, DataMigrationCatalog
from .database import PsqlClient
from .errors import ExecutionError, ImmutableArtifactError, PrerequisiteError
from .models import MigrationKind, MigrationPlan, MigrationState
from .schema_history import data_job_coverage

SAFE_PYTHON_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONUNBUFFERED",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class DataMigrationRunner:
    def __init__(
        self,
        catalog: DataMigrationCatalog,
        database: PsqlClient,
        *,
        environment: dict[str, str] | None = None,
        source_sha: str | None = None,
    ) -> None:
        self.catalog = catalog
        self.database = database
        self.environment = dict(os.environ if environment is None else environment)
        self.source_sha = source_sha or self._discover_source_sha()

    def plan(self, migration_id: str) -> MigrationPlan:
        artifact = self.catalog.get(migration_id)
        applied = self.database.applied_schema_versions()
        applied, retired = data_job_coverage(self.catalog.repository_root, applied, migration_id)
        missing_schema = sorted(set(artifact.manifest.requires_schema) - applied)
        if missing_schema:
            return MigrationPlan(
                id=migration_id,
                kind=artifact.manifest.kind,
                state=MigrationState.BLOCKED,
                checksum=artifact.checksum,
                legacy=artifact.manifest.legacy,
                missing_schema=missing_schema,
            )

        receipt = self.database.receipt(migration_id)
        if receipt is not None:
            receipt_checksum = receipt.get("artifact_checksum")
            if receipt_checksum != artifact.checksum:
                raise ImmutableArtifactError(
                    f"completed migration {migration_id} has checksum "
                    f"{receipt_checksum or '<missing>'}, repository has {artifact.checksum}"
                )
            return MigrationPlan(
                id=migration_id,
                kind=artifact.manifest.kind,
                state=MigrationState.COMPLETED,
                checksum=artifact.checksum,
                legacy=artifact.manifest.legacy,
                completed_source_sha=receipt.get("source_sha"),
                retired=retired,
            )

        if retired:
            return MigrationPlan(
                id=migration_id,
                kind=artifact.manifest.kind,
                state=MigrationState.BLOCKED,
                checksum=artifact.checksum,
                legacy=artifact.manifest.legacy,
                retired=True,
            )

        missing_environment = sorted(
            name for name in artifact.manifest.required_env if not self.environment.get(name)
        )
        state = MigrationState.BLOCKED if missing_environment else MigrationState.READY
        return MigrationPlan(
            id=migration_id,
            kind=artifact.manifest.kind,
            state=state,
            checksum=artifact.checksum,
            legacy=artifact.manifest.legacy,
            missing_environment=missing_environment,
        )

    def run(self, migration_id: str) -> MigrationPlan:
        plan = self.plan(migration_id)
        if plan.state is MigrationState.COMPLETED:
            return plan
        if plan.state is MigrationState.BLOCKED:
            reasons: list[str] = []
            if plan.retired:
                reasons.append("data migration retired after B1; use the archived upgrade path")
            if plan.missing_schema:
                reasons.append(f"missing schema: {', '.join(plan.missing_schema)}")
            if plan.missing_environment:
                reasons.append(f"missing environment: {', '.join(plan.missing_environment)}")
            raise PrerequisiteError("; ".join(reasons))

        artifact = self.catalog.get(migration_id)
        if artifact.manifest.kind is MigrationKind.SQL:
            self.database.run_sql_transaction(
                migration_id=migration_id,
                entrypoint_path=artifact.entrypoint_path,
                verify_path=artifact.verify_path,
                checksum=artifact.checksum,
                source_sha=self.source_sha,
                legacy=artifact.manifest.legacy,
                timeout=artifact.manifest.timeout_seconds,
            )
        else:
            self._run_python(artifact)
        return self.plan(migration_id)

    def verify(self, migration_id: str) -> None:
        plan = self.plan(migration_id)
        if plan.retired:
            raise PrerequisiteError(
                "data migration retired after B1; its old tables no longer exist"
            )
        if plan.missing_schema:
            raise PrerequisiteError(f"missing schema: {', '.join(plan.missing_schema)}")
        if plan.state is not MigrationState.COMPLETED:
            raise PrerequisiteError(
                f"completion receipt is missing for data migration: {migration_id}"
            )
        artifact = self.catalog.get(migration_id)
        self.database.verify(
            artifact.verify_path,
            timeout=artifact.manifest.timeout_seconds,
        )

    def verify_external_state(self, migration_id: str) -> dict[str, object]:
        """Verify operator-run work without inventing a runner completion receipt."""
        artifact = self.catalog.get(migration_id)
        applied, retired = data_job_coverage(
            self.catalog.repository_root, self.database.applied_schema_versions(), migration_id
        )
        if retired or set(artifact.manifest.requires_schema) - applied:
            raise PrerequisiteError("data migration is retired or its schema is missing")
        self.database.verify(artifact.verify_path, timeout=artifact.manifest.timeout_seconds)
        return {
            "id": migration_id,
            "verified": True,
            "verification": "external_state",
            "artifact_checksum": artifact.checksum,
            "source_sha": self.source_sha,
        }

    def _run_python(self, artifact: DataMigrationArtifact) -> None:
        manifest = artifact.manifest
        environment = self._python_environment(artifact)
        with self.database.advisory_lock(manifest.id):
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-u",
                        str(artifact.entrypoint_path),
                        "--apply",
                    ],
                    cwd=artifact.directory,
                    env=environment,
                    check=False,
                    text=True,
                    timeout=manifest.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise ExecutionError(
                    f"Python data migration timed out after "
                    f"{manifest.timeout_seconds} seconds: {manifest.id}"
                ) from error
            if result.returncode != 0:
                raise ExecutionError(
                    f"Python data migration exited with {result.returncode}: {manifest.id}"
                )
            self.database.verify(
                artifact.verify_path,
                timeout=manifest.timeout_seconds,
            )
            self.database.record_receipt(
                migration_id=manifest.id,
                checksum=artifact.checksum,
                source_sha=self.source_sha,
                legacy=manifest.legacy,
            )

    def _python_environment(self, artifact: DataMigrationArtifact) -> dict[str, str]:
        manifest = artifact.manifest
        environment = {
            name: value
            for name, value in self.environment.items()
            if name in SAFE_PYTHON_ENVIRONMENT or name in manifest.required_env
        }
        # The child runs with Python's isolated mode from the immutable artifact
        # directory. Do not add the repository or an operator checkout to its
        # import path: that would create code outside the artifact checksum.
        environment.pop("PYTHONPATH", None)
        if manifest.batch_size is not None:
            environment["DATA_MIGRATION_BATCH_SIZE"] = str(manifest.batch_size)
        return environment

    def _discover_source_sha(self) -> str:
        configured = self.environment.get("GITHUB_SHA")
        if configured:
            return configured[:64]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.catalog.repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:64]
        return "unknown"
