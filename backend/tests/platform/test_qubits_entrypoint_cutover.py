from __future__ import annotations

import importlib.util
import json
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


@pytest.fixture
def logical_backup():
    return {
        "format_version": 1, "project_ref": "project", "sha256": "a" * 64,
        "bytes": 4096, "created_at": "2026-01-01T00:02:00Z",
        "verified_at": "2026-01-01T00:03:00Z", "restore_verified": True,
        "retained_at": "private-operator-backup-archive",
        "restore_procedure": "application-schema-and-data-restore",
        "verification_record": "isolated-restoration-receipt",
        "verified_by": "release-operator",
    }


@pytest.mark.parametrize("change", [
    {"project_ref": "other"}, {"sha256": "b" * 64}, {"bytes": 0}, {"bytes": True},
    {"format_version": True}, {"restore_verified": False}, {"restore_verified": "true"},
    {"retained_at": ""}, {"verification_record": ""}, {"restore_procedure": ""},
    {"verified_by": ""}, {"created_at": "2026-01-01T00:00:00Z"},
    {"verified_at": "2026-01-01T00:01:00Z"}, {"verified_at": "9999-01-01T00:00:00Z"},
    {"created_at": "2026-01-01T00:02:00"}, {"created_at": None},
])
def test_logical_backup_rejects_wrong_target_unverified_or_stale_restore(adapter, logical_backup, change):
    logical_backup.update(change)
    with pytest.raises(ValueError, match="CUTOVER_"):
        adapter.verified_logical_backup(
            "project", "operator-logical:project:sha256:" + "a" * 64,
            {"environment": "qubits", "writers_stopped_at": "2026-01-01T00:01:00Z"}, logical_backup,
        )


@pytest.mark.parametrize("valid", [True, False])
def test_prepare_without_pitr_requires_verified_operator_backup_before_any_write(adapter, logical_backup, monkeypatch, valid):
    ref = "a" * 20
    logical_backup.update(project_ref=ref, restore_verified=valid)
    values = {
        "GITHUB_REF": "refs/heads/qubits", "TARGET_ENVIRONMENT": "staging",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": "operator",
        "SUPABASE_PROJECT_ID": ref, "SUPABASE_URL": f"https://{ref}.supabase.co",
        "SUPABASE_ACCESS_TOKEN": "fixture", "DATA_MIGRATION_DATABASE_URL": "fixture",
        "CUTOVER_OPERATION": "prepare",
        "ENTRYPOINT_CUTOVER_DECISIONS": json.dumps({"format_version": 1, "rows": []}),
        "ENTRYPOINT_CUTOVER_EVIDENCE": json.dumps({"environment": "qubits", "writers_stopped_at": "2026-01-01T00:01:00Z"}),
        "ENTRYPOINT_CUTOVER_RESTORE_POINT": f"operator-logical:{ref}:sha256:" + "a" * 64,
        "ENTRYPOINT_CUTOVER_LOGICAL_BACKUP": json.dumps(logical_backup),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    class Database:
        def __init__(self, _url): pass
        def assert_supabase_target(self, **_kwargs): pass
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"pitr_enabled": False}
    writes = []
    monkeypatch.setattr(adapter, "PsqlClient", Database)
    monkeypatch.setattr(adapter.httpx, "get", lambda *a, **k: Response())
    monkeypatch.setattr(adapter, "approve", lambda *a: writes.append("approve"))
    monkeypatch.setattr(adapter, "freeze", lambda *a, **k: writes.append("freeze"))
    if valid:
        adapter.main()
        assert writes == ["approve", "freeze"]
    else:
        with pytest.raises(ValueError, match="RESTORE_UNVERIFIED"):
            adapter.main()
        assert writes == []
