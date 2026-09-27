"""Regression contracts for grandfathered credential data migrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from src.infra.data_migrations.schema_history import data_migration_directory

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_scope_module():
    path = data_migration_directory(REPO_ROOT, "20260704_scope_access_key_hash") / "run.py"
    spec = importlib.util.spec_from_file_location("legacy_scope_hash_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_surface_module():
    path = data_migration_directory(REPO_ROOT, "20260711_surface_credentials") / "run.py"
    spec = importlib.util.spec_from_file_location("legacy_surface_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_applied_credential_sql_history_remains_language_neutral() -> None:
    for migration_name in (
        "20260711010000_harden_agent_sandbox_credentials.sql",
        "20260711070000_move_scope_credentials_to_access_credentials.sql",
    ):
        migration = (
            (REPO_ROOT / "supabase/archive/before_b1/migrations" / migration_name)
            .read_text(encoding="utf-8")
            .lower()
        )
        assert "python" not in migration
        assert ".py" not in migration
        assert "scripts/" not in migration


def test_credential_backfills_are_versioned_legacy_artifacts() -> None:
    expected = {
        "20260704_scope_access_key_hash": "20260704000000",
        "20260711_surface_credentials": "20260616003000",
    }
    for migration_id, required_schema in expected.items():
        directory = data_migration_directory(REPO_ROOT, migration_id)
        manifest = yaml.safe_load((directory / "manifest.yml").read_text())
        assert manifest["id"] == migration_id
        assert manifest["kind"] == "python"
        assert manifest["entrypoint"] == "run.py"
        assert manifest["verify"] == "verify.sql"
        assert manifest["legacy"] is True
        assert required_schema in manifest["requires_schema"]
        assert (directory / "run.py").is_file()
        assert (directory / "verify.sql").is_file()


def test_credential_backfills_use_bounded_stable_keyset_pagination() -> None:
    for migration_id in (
        "20260704_scope_access_key_hash",
        "20260711_surface_credentials",
    ):
        source = (data_migration_directory(REPO_ROOT, migration_id) / "run.py").read_text()
        assert '.order("id")' in source
        assert ".limit(page_size)" in source
        assert '.gt("id", after_id)' in source
        assert ".range(" not in source


def test_schema_deployment_never_names_application_backfills() -> None:
    for workflow_name in ("migrate-staging.yml", "migrate-production.yml"):
        workflow = (REPO_ROOT / ".github/workflows" / workflow_name).read_text()
        assert "backfill_scope_access_key_hash" not in workflow
        assert "backfill_surface_credentials" not in workflow
        # Resolving release metadata performs no data transformation.
        environment = "staging" if workflow_name == "migrate-staging.yml" else "production"
        metadata_command = (
            f"python3 scripts/database_history.py release --environment {environment}"
        )
        assert "scripts/" not in workflow.replace(metadata_command, "")
        assert "_schema-deploy.yml" in workflow

    reusable = (REPO_ROOT / ".github/workflows/_schema-deploy.yml").read_text()
    # Connection setup and verified history adoption never execute application
    # backfills. All other Python steps remain forbidden in the schema lane.
    without_connection = reusable.replace("python3 supabase/releases/connection.py", "")
    without_connection = without_connection.replace("python3 scripts/database_history.py adopt", "")
    assert "python" not in without_connection.lower()
    assert "backfill" not in reusable.lower()


def test_data_workflow_calls_only_the_portable_runner() -> None:
    reusable = (REPO_ROOT / ".github/workflows/_data-migration.yml").read_text()
    assert "puppyone-db plan" in reusable
    assert "puppyone-db run" in reusable
    assert "puppyone-db verify" in reusable
    assert "run.py" not in reusable
    assert "20260704_scope_access_key_hash" not in reusable
    assert "20260711_surface_credentials" not in reusable


def test_scope_backfill_only_skips_the_expected_retired_column_error() -> None:
    legacy_columns_have_been_retired = _load_scope_module()._legacy_columns_have_been_retired

    class PostgrestMissingColumn(Exception):
        code = "PGRST204"

    class PostgresMissingColumn(Exception):
        code = "42703"

    assert legacy_columns_have_been_retired(
        PostgrestMissingColumn(
            "Could not find the 'access_key' column of 'repo_scopes' in the schema cache"
        )
    )
    assert legacy_columns_have_been_retired(
        PostgresMissingColumn("column repo_scopes.access_key does not exist")
    )
    assert not legacy_columns_have_been_retired(
        PostgrestMissingColumn("Could not find the 'other_column' column of 'repo_scopes'")
    )
    assert not legacy_columns_have_been_retired(Exception("network unavailable"))


def test_surface_backfill_supports_the_pre_binding_credential_schema() -> None:
    surface_module = _load_surface_module()

    class MissingBindingColumn(Exception):
        code = "PGRST204"

    class Response:
        def __init__(self, data):
            self.data = data

    class Query:
        def __init__(self):
            self.selection = ""

        def select(self, selection):
            self.selection = selection
            return self

        def eq(self, *args):
            return self

        def is_(self, *args):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args):
            return self

        def execute(self):
            if "workspace_binding_id" in self.selection:
                raise MissingBindingColumn(
                    "Could not find the 'workspace_binding_id' column of "
                    "'access_surface_credentials' in the schema cache"
                )
            return Response([{"id": "legacy", "key_hash": "hash"}])

    class Client:
        def table(self, name):
            assert name == "access_surface_credentials"
            return Query()

    assert surface_module._active_credential(Client(), "surface") == {
        "id": "legacy",
        "key_hash": "hash",
    }


def test_surface_backfill_does_not_hide_unrelated_query_failures() -> None:
    surface_module = _load_surface_module()

    class Query:
        def select(self, *args):
            return self

        def eq(self, *args):
            return self

        def is_(self, *args):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args):
            return self

        def execute(self):
            raise RuntimeError("authentication failed")

    class Client:
        def table(self, name):
            return Query()

    with pytest.raises(RuntimeError, match="authentication failed"):
        surface_module._active_credential(Client(), "surface")
