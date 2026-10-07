from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("qubits_cutover_test", SCRIPTS / "qubits_entrypoint_cutover.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("enabled", "point", "stopped", "environment", "expected"),
    [
        (True, "supabase-pitr:project:200", "1970-01-01T00:03:00Z", "qubits", None),
        (False, "supabase-pitr:project:200", "1970-01-01T00:03:00Z", "qubits", "UNAVAILABLE"),
        (True, "supabase-pitr:other:200", "1970-01-01T00:03:00Z", "qubits", "REQUIRED"),
        (True, "supabase-pitr:project:99", "1970-01-01T00:00:00Z", "qubits", "UNAVAILABLE"),
        (True, "supabase-pitr:project:301", "1970-01-01T00:03:00Z", "qubits", "UNAVAILABLE"),
        (True, "supabase-pitr:project:200", "1970-01-01T00:04:00Z", "qubits", "PRECEDES_STOP"),
        (True, "supabase-pitr:project:200", "1970-01-01T00:03:00", "qubits", "PRECEDES_STOP"),
        (True, "supabase-pitr:project:200", "1970-01-01T00:03:00Z", "production", "WRONG_EVIDENCE_ENVIRONMENT"),
    ],
)
def test_recovery_point_matches_target_window_and_writer_stop(adapter, enabled, point, stopped, environment, expected):
    backups = {"pitr_enabled": enabled, "physical_backup_data": {
        "earliest_physical_backup_date_unix": 100,
        "latest_physical_backup_date_unix": 300,
    }}
    evidence = {"writers_stopped_at": stopped, "environment": environment}
    if expected:
        with pytest.raises(ValueError, match=expected):
            adapter.verified_pitr_point(backups, "project", point, evidence)
    else:
        adapter.verified_pitr_point(backups, "project", point, evidence)


def test_wrong_branch_rejected_before_reading_credentials(adapter, monkeypatch):
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("TARGET_ENVIRONMENT", "staging")
    monkeypatch.delenv("SUPABASE_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="WRONG_ENVIRONMENT"):
        adapter.main()
