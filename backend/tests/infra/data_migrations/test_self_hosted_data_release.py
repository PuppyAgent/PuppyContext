"""Installer data failure must prevent application admission, including on retry."""

import importlib
import json
from pathlib import Path
from unittest.mock import Mock

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
