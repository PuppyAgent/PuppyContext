from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "database_baseline", ROOT / "scripts/database_baseline.py"
)
assert SPEC and SPEC.loader
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)


def test_duplicate_versions_are_rejected(tmp_path, monkeypatch):
    for name in ("20260926000000_one.sql", "20260926000000_two.sql"):
        (tmp_path / name).write_text("SELECT 1;")
    monkeypatch.setattr(baseline, "MIGRATIONS", tmp_path)
    with pytest.raises(ValueError, match="Duplicate migration"):
        baseline.source_files()


def test_source_inventory_is_cutoff_bounded(tmp_path, monkeypatch):
    for name in ("20260926000000_one.sql", "20260927000000_two.sql"):
        (tmp_path / name).write_text("SELECT 1;")
    monkeypatch.setattr(baseline, "MIGRATIONS", tmp_path)
    assert [p.name for p in baseline.source_files("20260926000000")] == ["20260926000000_one.sql"]
    with pytest.raises(ValueError, match="cutoff"):
        baseline.source_files("20260925000000")


def test_dump_normalization_preserves_function_comments():
    first = "-- Dumped by pg_dump version 17.6\n\\restrict random\nCREATE FUNCTION x()\n-- function comment\nSELECT 1;\n\\unrestrict random\n"
    second = first.replace("17.6", "17.7").replace("random", "other")
    assert baseline.normalize_dump(first) == baseline.normalize_dump(second)
    assert "-- function comment" in baseline.normalize_dump(first)


def test_platform_acl_detection_never_filters_application_role_grants():
    platform = 'ALTER DEFAULT PRIVILEGES FOR ROLE "supabase_admin" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";'
    application = 'ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON TABLES FROM "anon";'
    assert baseline.managed_default_acls(platform + "\n" + application) == {platform}


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {"projects": 1},
        {baseline.INVENTORY_TABLE: 2},
        {baseline.INVENTORY_TABLE: 1, "profiles": 1},
    ],
)
def test_fresh_capture_rejects_missing_seed_or_user_data(counts):
    with pytest.raises(ValueError, match="Unreviewed"):
        baseline.validate_fresh_rows(counts)


def test_fresh_capture_accepts_only_reviewed_seed():
    baseline.validate_fresh_rows({baseline.INVENTORY_TABLE: 1, "projects": 0})


@pytest.mark.parametrize("component", ["schema", "auth_triggers", "reference_data"])
def test_equivalence_checks_schema_platform_and_data(component):
    original = {"schema": "DDL", "auth_triggers": "TRIGGER", "reference_data": {"seed": 1}}
    changed = {**original, component: "changed"}
    with pytest.raises(ValueError, match=component):
        baseline.verify_equivalence(original, changed)


def test_candidate_checksum_and_source_inventory_are_enforced(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    migration = migrations / "20260926000000_one.sql"
    migration.write_text("CREATE TABLE public.one(id int);")
    monkeypatch.setattr(baseline, "MIGRATIONS", migrations)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in ("baseline.sql", "required_data.sql"):
        (candidate / name).write_text("SELECT 1;")
    manifest = {
        "api_version": 1,
        "status": "candidate",
        "cutoff_version": "20260926000000",
        "source_migrations": {migration.name: baseline.digest(migration.read_bytes())},
        "files": {
            name: baseline.digest((candidate / name).read_bytes())
            for name in ("baseline.sql", "required_data.sql")
        },
    }
    (candidate / "manifest.json").write_text(json.dumps(manifest))
    baseline.validate_candidate(candidate)
    migration.write_text("DROP TABLE public.one;")
    with pytest.raises(ValueError, match="source migration inventory"):
        baseline.validate_candidate(candidate)
    migration.write_text("CREATE TABLE public.one(id int);")
    (candidate / "baseline.sql").write_text("SELECT 2;")
    with pytest.raises(ValueError, match="artifact changed"):
        baseline.validate_candidate(candidate)


def test_baseline_workflow_has_no_hosted_connection_or_write_permission():
    text = (ROOT / ".github/workflows/prepare-baseline.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "environment:" not in text
    assert "--linked" not in text
    assert "migration repair" not in text
    assert "scripts/database_baseline.py" in text


def test_checked_in_candidate_if_present():
    candidate = ROOT / "supabase/baselines/b1"
    if (candidate / "manifest.json").exists():
        baseline.validate_candidate(candidate)
