from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.infra.data_migrations.baseline_adoption import adoption_sql
from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.errors import ManifestError
from src.infra.data_migrations.policy import ChangedPath, validate_repository_policy
from src.infra.data_migrations.schema_history import (
    baseline_version,
    data_job_coverage,
    data_migration_directory,
    historical_source,
    load_baseline,
)

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def archived_repository(tmp_path):
    for name in ("migrations", "archive", "baselines", "data_migrations", "releases"):
        shutil.copytree(ROOT / "supabase" / name, tmp_path / "supabase" / name)
    shutil.copyfile(ROOT / "supabase/config.toml", tmp_path / "supabase/config.toml")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/pyproject.toml").write_text("")
    return tmp_path


def test_one_executable_baseline_and_complete_archive():
    baseline = load_baseline(ROOT)
    assert baseline
    assert len(baseline["source_migrations"]) == 102
    assert not (ROOT / "supabase/baselines/b1/baseline.sql").exists()
    for name in baseline["source_migrations"]:
        assert historical_source(ROOT, name).parent == ROOT / baseline["archive"]
    assert not any(
        (ROOT / "supabase/migrations" / n).exists() for n in baseline["source_migrations"]
    )


def test_archive_tampering_and_duplicate_execution_are_rejected(archived_repository):
    root = archived_repository
    baseline = load_baseline(root)
    name = next(iter(baseline["source_migrations"]))
    source = root / baseline["archive"] / name
    original = source.read_bytes()
    source.write_bytes(original + b"-- changed\n")
    with pytest.raises(ManifestError, match="checksum changed"):
        load_baseline(root)
    source.write_bytes(original)
    shutil.copyfile(source, root / "supabase/migrations" / name)
    with pytest.raises(ManifestError, match="remains in active"):
        load_baseline(root)


def test_policy_allows_exact_archive_move_but_rejects_later_archive_edits():
    baseline = load_baseline(ROOT)
    name = next(iter(baseline["source_migrations"]))
    validate_repository_policy(
        DataMigrationCatalog(ROOT),
        [
            ChangedPath("R100", "supabase/migrations/" + name),
            ChangedPath("R100", baseline["archive"] + "/" + name),
            ChangedPath("A", baseline["migration"]),
        ],
    )
    with pytest.raises(ManifestError, match="archived schema SQL is immutable"):
        validate_repository_policy(
            DataMigrationCatalog(ROOT),
            [
                ChangedPath("M", baseline["archive"] + "/" + name),
            ],
        )


def test_data_prerequisites_are_covered_without_manufacturing_receipts():
    baseline = load_baseline(ROOT)
    active = {baseline_version(baseline)}
    covered, retired = data_job_coverage(ROOT, active, "20260720_project_storage_inventory")
    assert "20260720000000" in covered
    assert not retired
    _, retired = data_job_coverage(
        ROOT, active, "20260715_project_owned_repository_targets_preflight"
    )
    assert retired
    assert data_job_coverage(ROOT, {"20260720000000"}, "20260720_project_storage_inventory") == (
        {"20260720000000"},
        False,
    )


def test_adoption_requires_reviewed_fingerprint_and_atomic_history_archive(archived_repository):
    path = archived_repository / "supabase/baselines/b1/manifest.json"
    manifest = json.loads(path.read_text())
    manifest.pop("schema_fingerprint", None)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="not been verified"):
        adoption_sql(archived_repository, apply=True)
    sql = adoption_sql(archived_repository, apply=True, fingerprint="a" * 64)
    assert sql.index("BASELINE_SCHEMA_DRIFT") < sql.index("DELETE FROM supabase_migrations")
    assert sql.index("'original_history',original_history") < sql.index(
        "DELETE FROM supabase_migrations"
    )
    assert sql.rstrip().endswith("COMMIT;")
    assert (
        adoption_sql(archived_repository, apply=False, fingerprint="a" * 64)
        .rstrip()
        .endswith("ROLLBACK;")
    )
    assert "DROP TABLE" not in sql and "TRUNCATE" not in sql


def test_deploy_admits_history_before_native_supabase_push():
    workflow = (ROOT / ".github/workflows/_schema-deploy.yml").read_text()
    assert workflow.index("scripts/database_history.py adopt") < workflow.index("supabase db push")


def test_schema_tooling_needs_no_application_site_packages():
    for script in ("database_history.py", "database_baseline.py"):
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts" / script), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_new_migration_cannot_sort_before_b1(archived_repository):
    path = archived_repository / "supabase/migrations/20260925000000_out_of_order.sql"
    path.write_text("SELECT 1;\n")
    with pytest.raises(ManifestError, match="must follow the active baseline"):
        validate_repository_policy(DataMigrationCatalog(archived_repository), [])


def test_all_pre_b1_data_artifacts_resolve_without_changing_receipt_identity():
    baseline = load_baseline(ROOT)
    artifacts = DataMigrationCatalog(ROOT).load_all()
    archived = [item for item in artifacts if item.manifest.id in baseline["source_data_migrations"]]
    assert len(archived) == len(baseline["source_data_migrations"]) == 8
    for artifact in archived:
        migration_id = artifact.manifest.id
        assert artifact.directory == data_migration_directory(ROOT, migration_id)
        assert artifact.directory.parent == ROOT / baseline["data_archive"]
        assert (
            artifact.checksum
            == baseline["source_data_migrations"][migration_id]["artifact_checksum"]
        )


@pytest.mark.parametrize("operation", ["edit", "delete", "add", "duplicate"])
def test_archived_data_artifacts_cannot_be_rewritten(archived_repository, operation):
    root = archived_repository
    migration_id = "20260720_project_storage_inventory"
    directory = data_migration_directory(root, migration_id)
    if operation == "edit":
        path = directory / "verify.sql"
        path.write_text(path.read_text() + "\n-- changed")
    elif operation == "delete":
        (directory / "README.md").unlink()
    elif operation == "add":
        (directory / "unexpected.sql").write_text("SELECT 1;")
    else:
        shutil.copytree(directory, root / "supabase/data_migrations" / migration_id)
    with pytest.raises(ManifestError):
        DataMigrationCatalog(root).load_all()


def test_archived_completed_job_is_not_reexecuted():
    from src.infra.data_migrations.models import MigrationState
    from src.infra.data_migrations.runner import DataMigrationRunner

    from .test_runner import FakeDatabase

    catalog = DataMigrationCatalog(ROOT)
    artifact = catalog.get("20260713_reconcile_project_creator_admin")
    database = FakeDatabase()
    database.versions = {baseline_version(load_baseline(ROOT))}
    database.receipts[artifact.manifest.id] = {"artifact_checksum": artifact.checksum}
    runner = DataMigrationRunner(catalog, database, environment={}, source_sha="test")
    assert runner.run(artifact.manifest.id).state is MigrationState.COMPLETED
    assert database.sql_runs == []


def test_current_release_resolves_without_database_or_site_packages():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "scripts/database_history.py"),
            "release",
            "--environment",
            "staging",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    pointer = json.loads((ROOT / "supabase/releases/staging-data-migration.json").read_text())
    assert f"migration_id={pointer['migration_id']}\n" in result.stdout
    assert f"execution_mode={pointer['execution_mode']}\n" in result.stdout
