from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.errors import ImmutableArtifactError, PrerequisiteError
from src.infra.data_migrations.models import MigrationState
from src.infra.data_migrations.runner import DataMigrationRunner

from .test_catalog import _repository


class FakeDatabase:
    def __init__(self) -> None:
        self.versions = {"20260712010000"}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.sql_runs: list[dict[str, Any]] = []
        self.verified: list[Path] = []

    def applied_schema_versions(self) -> set[str]:
        return self.versions

    def receipt(self, migration_id: str) -> dict[str, Any] | None:
        return self.receipts.get(migration_id)

    def run_sql_transaction(self, **kwargs: Any) -> None:
        self.sql_runs.append(kwargs)
        self.receipts[kwargs["migration_id"]] = {
            "artifact_checksum": kwargs["checksum"],
            "source_sha": kwargs["source_sha"],
        }

    def verify(self, verify_path: Path, *, timeout: int) -> None:
        self.verified.append(verify_path)

    def advisory_lock(self, migration_id: str):
        return nullcontext()

    def record_receipt(
        self,
        *,
        migration_id: str,
        checksum: str,
        source_sha: str,
        legacy: bool,
    ) -> None:
        self.receipts[migration_id] = {
            "artifact_checksum": checksum,
            "source_sha": source_sha,
            "legacy": legacy,
        }


def test_plan_blocks_before_required_schema_exists(tmp_path: Path) -> None:
    catalog = DataMigrationCatalog(_repository(tmp_path))
    database = FakeDatabase()
    database.versions.clear()
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="abc")

    plan = runner.plan("20260712_example")

    assert plan.state is MigrationState.BLOCKED
    assert plan.missing_schema == ["20260712010000"]
    with pytest.raises(PrerequisiteError, match="missing schema"):
        runner.run("20260712_example")


def test_sql_run_is_transactional_and_becomes_idempotent(tmp_path: Path) -> None:
    catalog = DataMigrationCatalog(_repository(tmp_path))
    database = FakeDatabase()
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="abc")

    first = runner.run("20260712_example")
    second = runner.run("20260712_example")

    assert first.state is MigrationState.COMPLETED
    assert second.state is MigrationState.COMPLETED
    assert len(database.sql_runs) == 1
    assert database.sql_runs[0]["source_sha"] == "abc"


def test_completed_id_with_different_checksum_fails_closed(tmp_path: Path) -> None:
    catalog = DataMigrationCatalog(_repository(tmp_path))
    database = FakeDatabase()
    database.receipts["20260712_example"] = {
        "artifact_checksum": "0" * 64,
        "source_sha": "old",
    }
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="new")

    with pytest.raises(ImmutableArtifactError, match="checksum"):
        runner.plan("20260712_example")


def test_completed_migration_does_not_require_runtime_secret(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manifest = repository / "supabase/data_migrations/20260712_example/manifest.yml"
    manifest.write_text(
        manifest.read_text().replace("required_env: []", "required_env:\n  - APP_SECRET")
    )
    catalog = DataMigrationCatalog(repository)
    artifact = catalog.get("20260712_example")
    database = FakeDatabase()
    database.receipts["20260712_example"] = {
        "artifact_checksum": artifact.checksum,
        "source_sha": "old",
    }
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="new")

    assert runner.plan("20260712_example").state is MigrationState.COMPLETED


def test_verify_requires_a_durable_completion_receipt(tmp_path: Path) -> None:
    catalog = DataMigrationCatalog(_repository(tmp_path))
    database = FakeDatabase()
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="abc")

    with pytest.raises(PrerequisiteError, match="completion receipt"):
        runner.verify("20260712_example")


def test_external_verification_checks_state_without_creating_a_receipt(tmp_path: Path):
    catalog = DataMigrationCatalog(_repository(tmp_path))
    database = FakeDatabase()
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="exact-head")
    result = runner.verify_external_state("20260712_example")
    assert result["artifact_checksum"] == catalog.get("20260712_example").checksum
    assert result["source_sha"] == "exact-head"
    assert result["verification"] == "external_state"
    assert database.verified == [catalog.get("20260712_example").verify_path]
    assert not database.receipts and not database.sql_runs


def test_external_verification_rejects_missing_schema_and_failed_postcondition(tmp_path: Path):
    catalog = DataMigrationCatalog(_repository(tmp_path))
    database = FakeDatabase()
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="test")
    database.versions.clear()
    with pytest.raises(PrerequisiteError, match="schema is missing"):
        runner.verify_external_state("20260712_example")
    assert not database.verified
    database.versions = {"20260712010000"}

    def fail(*args, **kwargs):
        raise PrerequisiteError("postcondition failed")

    database.verify = fail
    with pytest.raises(PrerequisiteError, match="postcondition failed"):
        runner.verify_external_state("20260712_example")
    assert not database.receipts


def test_explicit_empty_environment_does_not_inherit_process_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    manifest = repository / "supabase/data_migrations/20260712_example/manifest.yml"
    manifest.write_text(
        manifest.read_text().replace("required_env: []", "required_env:\n  - APP_SECRET")
    )
    monkeypatch.setenv("APP_SECRET", "must-not-leak")
    runner = DataMigrationRunner(
        DataMigrationCatalog(repository),
        FakeDatabase(),
        environment={},
        source_sha="abc",
    )

    assert runner.plan("20260712_example").missing_environment == ["APP_SECRET"]


def test_python_child_receives_only_declared_secrets_and_safe_runtime(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    migration = repository / "supabase/data_migrations/20260712_example"
    manifest = migration / "manifest.yml"
    manifest.write_text(
        manifest.read_text()
        .replace("kind: sql", "kind: python")
        .replace("entrypoint: run.sql", "entrypoint: run.py")
        .replace("required_env: []", "required_env:\n  - APP_SECRET")
        + "batch_size: 250\n"
    )
    migration.joinpath("run.py").write_text("print('ok')\n")
    catalog = DataMigrationCatalog(repository)
    artifact = catalog.get("20260712_example")
    runner = DataMigrationRunner(
        catalog,
        FakeDatabase(),
        environment={
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://proxy.test",
            "APP_SECRET": "declared",
            "DATA_MIGRATION_DATABASE_URL": "must-not-leak",
            "GITHUB_TOKEN": "must-not-leak",
            "PYTHONPATH": "/untrusted/checkout",
        },
        source_sha="abc",
    )

    child = runner._python_environment(artifact)

    assert child["APP_SECRET"] == "declared"
    assert child["HTTPS_PROXY"] == "http://proxy.test"
    assert child["DATA_MIGRATION_BATCH_SIZE"] == "250"
    assert "PYTHONPATH" not in child
    assert "DATA_MIGRATION_DATABASE_URL" not in child
    assert "GITHUB_TOKEN" not in child


def test_python_job_runs_isolated_from_the_application_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository(tmp_path)
    migration = repository / "supabase/data_migrations/20260712_example"
    manifest = migration / "manifest.yml"
    manifest.write_text(
        manifest.read_text()
        .replace("kind: sql", "kind: python")
        .replace("entrypoint: run.sql", "entrypoint: run.py")
    )
    migration.joinpath("run.py").write_text("print('ok')\n")
    calls: list[dict[str, Any]] = []

    def fake_run(arguments, **kwargs):
        calls.append({"arguments": arguments, **kwargs})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("src.infra.data_migrations.runner.subprocess.run", fake_run)
    database = FakeDatabase()
    runner = DataMigrationRunner(
        DataMigrationCatalog(repository),
        database,
        environment={"PYTHONPATH": "/untrusted/checkout"},
        source_sha="abc",
    )

    result = runner.run("20260712_example")

    assert result.state is MigrationState.COMPLETED
    assert calls[0]["arguments"][1:4] == ["-I", "-B", "-u"]
    assert calls[0]["arguments"][-1] == "--apply"
    assert calls[0]["cwd"] == migration
    assert "PYTHONPATH" not in calls[0]["env"]
