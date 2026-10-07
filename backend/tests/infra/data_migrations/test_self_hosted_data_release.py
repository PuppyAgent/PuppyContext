"""Installer data failure must prevent application admission, including on retry."""

import importlib
import json
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def installer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("self_hosted_migrate")


def test_completed_receipts_are_reverified_before_startup(installer, monkeypatch):
    runner = Mock()
    monkeypatch.setattr(installer, "DataMigrationRunner", Mock(return_value=runner))
    installer.migrate_data(ROOT, Mock())
    migration_id = json.loads(
        (ROOT / "supabase/releases/standalone-data-migration.json").read_text()
    )["migration_id"]
    assert runner.method_calls == [
        ("run", (migration_id,), {}),
        ("verify", (migration_id,), {}),
    ]
    runner.verify.side_effect = RuntimeError("stored rows drifted")
    with pytest.raises(RuntimeError, match="stored rows drifted"):
        installer.migrate_data(ROOT, Mock())


def test_operator_release_is_never_silently_skipped(installer, monkeypatch):
    monkeypatch.setattr(
        installer, "resolve_release", lambda *_: {"execution_mode": "operator_local"}
    )
    with pytest.raises(RuntimeError, match="explicit operator"):
        installer.migrate_data(ROOT, Mock())


def test_runner_dependency_export_uses_the_backend_lock(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    exporter = importlib.import_module("migration_dependencies")
    output = exporter.requirements(ROOT / "backend/uv.lock")
    names = {line.split("==")[0] for line in output.splitlines()}
    assert {"pydantic", "pydantic-core", "pyyaml"} <= names
    assert "fastapi" not in names
    assert all("--hash=sha256:" in line for line in output.splitlines())


def test_installer_completes_declared_data_gate_before_retrying_schema(installer, monkeypatch):
    runner = Mock()
    monkeypatch.setattr(installer, "DataMigrationRunner", Mock(return_value=runner))
    process = Mock(side_effect=[
        SimpleNamespace(returncode=1, stdout="", stderr="DATA_MIGRATION_REQUIRED:20260927_entrypoint_storage_backfill"),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ])
    monkeypatch.setattr(installer.subprocess, "run", process)
    installer.push_schema(ROOT, Mock(), "postgresql://postgres@db/postgres", "test-only")
    assert runner.method_calls == [
        ("run", ("20260927_entrypoint_storage_backfill",), {}),
        ("verify", ("20260927_entrypoint_storage_backfill",), {}),
    ]
    assert process.call_count == 2


@pytest.mark.parametrize("failure", ["connection failed", "DATA_MIGRATION_REQUIRED:unknown_artifact"])
def test_installer_never_runs_data_for_an_unapproved_failure(installer, monkeypatch, failure):
    runner = Mock()
    monkeypatch.setattr(installer, "DataMigrationRunner", Mock(return_value=runner))
    monkeypatch.setattr(installer.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr=failure,
    )))
    with pytest.raises(RuntimeError, match="outside an approved data gate"):
        installer.push_schema(ROOT, Mock(), "postgresql://postgres@db/postgres", "test-only")
    assert runner.method_calls == []
