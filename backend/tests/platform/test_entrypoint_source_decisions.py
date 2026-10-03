"""Offline operator-boundary tests; database semantics live in the real SQL rehearsal."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/entrypoint_source_decisions.py"
spec = importlib.util.spec_from_file_location("entrypoint_decisions_under_test", SCRIPT)
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


def decision(**overrides):
    return {
        "legacy_id": "binding",
        "project_id": "project",
        "snapshot_sha256": "a" * 64,
        "disposition": "synchronize",
        "import_database_source_id": None,
        "binding_read_only_reason": None,
        "evidence_ref": "reviewed-caller-records",
        "approved_by": "fixture-reviewer",
        **overrides,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"disposition": None},
        {"disposition": "manual"},
        {"snapshot_sha256": "not-a-digest"},
        {"approved_by": " "},
        {"evidence_ref": None},
        {"legacy_id": ""},
        {"project_id": ""},
        {"disposition": "synchronize", "import_database_source_id": "inferred-source"},
        {"disposition": "import", "import_database_source_id": "changed-id"},
        {"disposition": "both", "import_database_source_id": None},
        {
            "disposition": "import",
            "import_database_source_id": "binding",
            "binding_read_only_reason": "cannot-retain-removed-binding",
        },
        {
            "disposition": "both",
            "import_database_source_id": "source",
            "binding_read_only_reason": " ",
        },
        {"config": {"secret": "must-not-be-copied"}},
    ],
)
def test_requires_explicit_review_without_inference(change):
    with pytest.raises(ValueError):
        operator.reviewed_rows({"format_version": 1, "rows": [decision(**change)]})


def test_duplicate_identity_or_source_rejected():
    with pytest.raises(ValueError, match="Duplicate legacy"):
        operator.reviewed_rows({"format_version": 1, "rows": [decision(), decision()]})
    rows = [
        decision(disposition="both", import_database_source_id="source", legacy_id=id_)
        for id_ in ("a", "b")
    ]
    with pytest.raises(ValueError, match="duplicate Import"):
        operator.reviewed_rows({"format_version": 1, "rows": rows})


def test_only_declared_review_fields_are_recorded():
    row = decision(
        disposition="both",
        import_database_source_id="independent-source",
        facts={"provider": "database"},
    )
    assert operator.reviewed_rows({"format_version": 1, "rows": [row]}) == [
        {k: v for k, v in row.items() if k != "facts"}
    ]
    with pytest.raises(ValueError):
        operator.reviewed_rows({"format_version": True, "rows": []})


@pytest.mark.parametrize(
    "change",
    [
        {"producer_stop_verified": False},
        {"queue_drain_verified": 1},
        {"old_consumers_exited": None},
        {"records": []},
        {"records": "claimed"},
        {"records": [None]},
        {"environment": " "},
    ],
)
def test_freeze_rejects_incomplete_evidence_without_contacting_database(change):
    db = Mock()
    evidence = {
        "producer_stop_verified": True,
        "queue_drain_verified": True,
        "old_consumers_exited": True,
        "environment": "test",
        "records": [{"fixture_only": True}],
        **change,
    }
    with pytest.raises(ValueError):
        operator.freeze(
            db,
            json.dumps(evidence).encode(),
            restore_point_ref="restore-ref",
            approved_by="reviewer",
        )
    db.scalar.assert_not_called()


def test_remote_default_rejected_before_client_or_network(monkeypatch, tmp_path):
    client = Mock()
    monkeypatch.setattr(operator, "PsqlClient", client)
    monkeypatch.setenv(
        "DATA_MIGRATION_DATABASE_URL", "postgresql://user:password@remote.example/postgres"
    )
    monkeypatch.setattr(
        operator.sys, "argv", [str(SCRIPT), "inventory", "--file", str(tmp_path / "inventory.json")]
    )
    with pytest.raises(ValueError, match="non-loopback"):
        operator.main()
    client.assert_not_called()


def test_inventory_is_new_private_file_and_never_overwrites(monkeypatch, tmp_path):
    client = Mock()
    client.return_value.scalar.return_value = "[]"
    monkeypatch.setattr(operator, "PsqlClient", client)
    monkeypatch.setenv("DATA_MIGRATION_DATABASE_URL", "postgresql://user@127.0.0.1/postgres")
    output = tmp_path / "inventory.json"
    monkeypatch.setattr(operator.sys, "argv", [str(SCRIPT), "inventory", "--file", str(output)])
    operator.main()
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {"format_version": 1, "rows": []}
    with pytest.raises(FileExistsError):
        operator.main()
    assert json.loads(output.read_text())["rows"] == []


def test_write_requires_explicit_apply(monkeypatch, tmp_path):
    client = Mock()
    monkeypatch.setattr(operator, "PsqlClient", client)
    monkeypatch.setenv("DATA_MIGRATION_DATABASE_URL", "postgresql://user@localhost/postgres")
    monkeypatch.setattr(
        operator.sys, "argv", [str(SCRIPT), "approve", "--file", str(tmp_path / "missing.json")]
    )
    with pytest.raises(ValueError, match="--apply"):
        operator.main()
    client.return_value.scalar.assert_not_called()
