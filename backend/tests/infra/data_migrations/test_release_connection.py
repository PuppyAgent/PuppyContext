from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.errors import PrerequisiteError

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "release_connection", ROOT / "supabase/releases/connection.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
REF = "a" * 20


def test_staging_rejects_a_mislabeled_project_before_network_access(monkeypatch):
    monkeypatch.setattr(module, "urlopen", lambda *a, **kw: pytest.fail("network used"))
    with pytest.raises(ValueError, match="Qubits test project"):
        module.connection_environment(
            {
                "TARGET_ENVIRONMENT": "staging",
                "SUPABASE_PROJECT_ID": REF,
                "SUPABASE_ACCESS_TOKEN": "must-not-be-used",
            }
        )


def test_staging_accepts_only_its_matching_database_connection():
    ref = module.STAGING_PROJECT_REF
    result = module.connection_environment(
        {
            "TARGET_ENVIRONMENT": "staging",
            "SUPABASE_PROJECT_ID": ref,
            "DATABASE_URL": f"postgresql://postgres:fixture@db.{ref}.supabase.co:5432/postgres",
        }
    )
    assert result["PGHOST"] == f"db.{ref}.supabase.co"
    with pytest.raises(ValueError, match="protected project"):
        module.connection_environment(
            {
                "TARGET_ENVIRONMENT": "staging",
                "SUPABASE_PROJECT_ID": ref,
                "DATABASE_URL": f"postgresql://postgres:fixture@db.{REF}.supabase.co:5432/postgres",
            }
        )


def test_static_connection_keeps_password_out_of_cli_arguments():
    result = module.connection_environment(
        {
            "SUPABASE_PROJECT_ID": REF,
            "DATABASE_URL": f"postgresql://postgres.{REF}:hello%40world@aws-0.pooler.supabase.com:5432/postgres?sslmode=require",
        }
    )
    assert result["PGPASSWORD"] == "hello@world"
    assert result["PGDATABASE"] == "postgres"
    assert "hello" not in result["SUPABASE_DATABASE_URL"]
    assert result["PGUSER"] == "postgres." + REF


@pytest.mark.parametrize(
    "uri",
    [
        "postgresql://postgres.other:secret@aws-0.pooler.supabase.com:5432/postgres",
        f"postgresql://postgres:secret@db.{REF}.supabase.co:6543/postgres",
        "postgresql://postgres:secret@attacker.invalid:5432/postgres",
    ],
)
def test_connection_rejects_wrong_project_or_transaction_pooler(uri):
    with pytest.raises(ValueError, match="protected project"):
        module.connection_environment({"SUPABASE_PROJECT_ID": REF, "DATABASE_URL": uri})


def test_expiring_cli_login_must_match_same_protected_project(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    client = PsqlClient(
        f"postgresql://cli_login_abc.{REF}:secret@aws-0.pooler.supabase.com:5432/postgres"
    )
    client.assert_supabase_target(project_ref=REF, api_url=f"https://{REF}.supabase.co")
    with pytest.raises(PrerequisiteError):
        client.assert_supabase_target(
            project_ref="b" * 20, api_url="https://" + "b" * 20 + ".supabase.co"
        )


def test_unexpired_official_credentials_are_reused_without_network(monkeypatch):
    import time

    monkeypatch.setattr(
        module, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected renewal")
    )
    result = module.connection_environment(
        {
            "SUPABASE_PROJECT_ID": REF,
            "DATABASE_URL": f"postgresql://cli_login_abc.{REF}:secret@aws-0.pooler.supabase.com:5432/postgres",
            "DATABASE_TEMPORARY": "1",
            "DATABASE_CREDENTIAL_EXPIRES_AT": str(time.time() + 250),
        }
    )
    assert result["PGUSER"] == "cli_login_abc." + REF
    assert result["DATABASE_TEMPORARY"] == "1"
