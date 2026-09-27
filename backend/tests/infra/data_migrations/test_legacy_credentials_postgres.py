"""Real PostgreSQL checks for the historical credential upgrade boundary.

The artifact's Supabase transport is adapted to PostgreSQL in one component
test; its actual pagination, HMAC, and retry code and the released SQL execute.
This does not replace a full production-copy upgrade rehearsal.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.errors import ExecutionError

ROOT = Path(__file__).resolve().parents[4]
PREFLIGHT = ROOT / "supabase/releases/legacy_credential_preflight.sql"
MIGRATIONS = ROOT / "supabase/archive/before_b1/migrations"
HASH_EXPAND = MIGRATIONS / "20260704000000_repo_scopes_access_key_hash.sql"
RETIRE = MIGRATIONS / "20260711070000_move_scope_credentials_to_access_credentials.sql"
EMPTY_FIELDS = (
    ROOT
    / "supabase/archive/before_b1/data_migrations/20260923_remove_empty_runtime_credential_fields"
)


@pytest.fixture
def database():
    url = os.environ.get("TEST_LEGACY_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_LEGACY_MIGRATION_DATABASE_URL to an isolated local PostgreSQL")
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("credential upgrade tests only accept a loopback PostgreSQL server")
    admin = PsqlClient(url)
    name = "puppy_credential_test_" + uuid4().hex
    admin.scalar(f"CREATE DATABASE {name}")
    db = PsqlClient(urlunsplit(parsed._replace(path="/" + name)))
    try:
        yield db
    finally:
        admin.scalar(f"DROP DATABASE {name} WITH (FORCE)")


def execute_file(database, path):
    return database.command(["-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)])


def seed(database):
    execute_file(database, ROOT / "supabase/test_fixtures/legacy_credential_upgrade.sql")


def test_empty_database_admits_schema_bootstrap(database):
    execute_file(database, PREFLIGHT)
    assert database.scalar("SELECT to_regclass('public.repo_scopes') IS NULL") == "t"


@pytest.mark.parametrize("expanded", [False, True])
def test_152_unprepared_credentials_stop_before_any_schema_write(database, expanded):
    seed(database)
    if expanded:
        execute_file(database, HASH_EXPAND)
    with pytest.raises(ExecutionError, match="LEGACY_CREDENTIAL_UPGRADE_REQUIRED: 152"):
        execute_file(database, PREFLIGHT)
    assert database.scalar("SELECT count(*) FROM public.repo_scopes") == "152"
    assert database.scalar("SELECT count(*) FROM public.access_surface_credentials") == "2"
    expected_columns = "7" if expanded else "6"
    assert (
        database.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='repo_scopes'"
        )
        == expected_columns
    )


def test_partial_hash_population_is_not_treated_as_ready(database):
    seed(database)
    execute_file(database, HASH_EXPAND)
    database.scalar(
        "UPDATE public.repo_scopes SET access_key_hash = repeat('a',64) WHERE id='scope-001'"
    )
    with pytest.raises(ExecutionError, match="LEGACY_CREDENTIAL_UPGRADE_REQUIRED: 151"):
        execute_file(database, PREFLIGHT)


@pytest.mark.parametrize("kind", ["agent", "sandbox"])
def test_surface_secret_retirement_is_checked_without_reading_values(database, kind):
    seed(database)
    database.scalar("DELETE FROM public.repo_scopes")
    database.scalar(
        "INSERT INTO public.access_surfaces(id,project_id,kind,config) VALUES ('legacy-surface','fixture-project',:'kind','{\"api_key\":\"synthetic-private-value\"}')",
        variables={"kind": kind},
    )
    with pytest.raises(ExecutionError, match="Agent/Sandbox") as failure:
        execute_file(database, PREFLIGHT)
    assert "synthetic-private-value" not in str(failure.value)


def test_released_sql_rejects_unhashed_credentials(database):
    seed(database)
    execute_file(database, HASH_EXPAND)
    with pytest.raises(ExecutionError, match="legacy credential backfill is incomplete"):
        execute_file(database, RETIRE)
    assert database.scalar("SELECT count(*) FROM public.repo_scopes") == "152"
    assert database.scalar("SELECT count(*) FROM public.access_surface_credentials") == "2"


@pytest.mark.parametrize("kind", ["agent", "sandbox"])
def test_empty_secret_placeholders_are_removed_without_changing_credentials(database, kind):
    seed(database)
    credentials = database.scalar(
        "SELECT jsonb_agg(c ORDER BY id) FROM public.access_surface_credentials c"
    )
    database.scalar(
        "INSERT INTO public.access_surfaces(id,project_id,kind,config) "
        "VALUES ('empty-secrets','fixture-project',:'kind', "
        '\'{"api_key":null,"access_key":"","mcp_api_key":null,'
        '"model":"preserved","nested":{"api_key":null}}\')',
        variables={"kind": kind},
    )
    with pytest.raises(ExecutionError, match="placeholder cleanup is incomplete"):
        execute_file(database, EMPTY_FIELDS / "verify.sql")
    execute_file(database, EMPTY_FIELDS / "run.sql")
    execute_file(database, EMPTY_FIELDS / "verify.sql")
    expected = {"model": "preserved", "nested": {"api_key": None}}
    assert (
        json.loads(
            database.scalar("SELECT config FROM public.access_surfaces WHERE id='empty-secrets'")
        )
        == expected
    )
    execute_file(database, EMPTY_FIELDS / "run.sql")
    assert (
        json.loads(
            database.scalar("SELECT config FROM public.access_surfaces WHERE id='empty-secrets'")
        )
        == expected
    )
    assert (
        database.scalar("SELECT jsonb_agg(c ORDER BY id) FROM public.access_surface_credentials c")
        == credentials
    )


def test_empty_placeholder_cleanup_preserves_nonempty_and_unrelated_values(database):
    seed(database)
    original = {
        "api_key": "synthetic-credential",
        "access_key": False,
        "mcp_api_key": " ",
        "other": None,
    }
    database.scalar(
        "INSERT INTO public.access_surfaces(id,project_id,kind,config) "
        "VALUES ('real-secret','fixture-project','agent',:'config'::jsonb), "
        "('other-kind','fixture-project','cli','{\"api_key\":null}')",
        variables={"config": json.dumps(original)},
    )
    execute_file(database, EMPTY_FIELDS / "run.sql")
    execute_file(database, EMPTY_FIELDS / "verify.sql")
    assert (
        json.loads(
            database.scalar("SELECT config FROM public.access_surfaces WHERE id='real-secret'")
        )
        == original
    )
    assert json.loads(
        database.scalar("SELECT config FROM public.access_surfaces WHERE id='other-kind'")
    ) == {"api_key": None}
    # Cleanup success cannot stand in for the actual credential backfill.
    database.scalar("DELETE FROM public.repo_scopes")
    with pytest.raises(ExecutionError, match="Agent/Sandbox"):
        execute_file(database, PREFLIGHT)


def test_actual_hash_artifact_then_retirement_preserves_tokens_and_existing_credentials(
    database, monkeypatch
):
    seed(database)
    execute_file(database, HASH_EXPAND)
    existing = database.scalar(
        "SELECT jsonb_agg(c ORDER BY id) FROM public.access_surface_credentials c"
    )
    path = ROOT / "supabase/archive/before_b1/data_migrations/20260704_scope_access_key_hash/run.py"
    spec = importlib.util.spec_from_file_location("postgres_legacy_scope_hash", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv(
        "ACCESS_CREDENTIAL_HASH_SECRET", "synthetic-isolated-upgrade-test-hmac-secret"
    )
    monkeypatch.setenv("DATA_MIGRATION_BATCH_SIZE", "17")

    class Updates:
        def table(self, name):
            assert name == "repo_scopes"
            return self

        def update(self, value):
            self.digest = value["access_key_hash"]
            return self

        def eq(self, column, value):
            assert column == "id"
            self.id = value
            return self

        def execute(self):
            database.scalar(
                "UPDATE public.repo_scopes SET access_key_hash=:'digest' WHERE id=:'id'",
                variables={"id": self.id, "digest": self.digest},
            )
            return SimpleNamespace(data=[])

    def pending_page(client, *, after_id, page_size):
        result = database.scalar(
            "SELECT COALESCE(jsonb_agg(s ORDER BY id),'[]') FROM (SELECT id,access_key,access_key_hash FROM public.repo_scopes WHERE access_key IS NOT NULL AND access_key_hash IS NULL AND id > :'after_id' ORDER BY id LIMIT :'size'::int) s",
            variables={"after_id": after_id or "", "size": str(page_size)},
        )
        return json.loads(result)

    monkeypatch.setattr(module, "_client", Updates)
    monkeypatch.setattr(module, "_pending_page", pending_page)
    module.backfill(apply=True)
    first = database.scalar("SELECT jsonb_agg(s ORDER BY id) FROM public.repo_scopes s")
    module.backfill(apply=True)
    assert database.scalar("SELECT jsonb_agg(s ORDER BY id) FROM public.repo_scopes s") == first
    assert (
        database.scalar("SELECT count(*) FROM public.repo_scopes WHERE access_key_hash IS NOT NULL")
        == "152"
    )
    execute_file(database, PREFLIGHT)
    execute_file(database, RETIRE)
    execute_file(database, PREFLIGHT)
    assert (
        database.scalar(
            "SELECT jsonb_agg(c ORDER BY id) FROM public.access_surface_credentials c WHERE id LIKE 'existing-%'"
        )
        == existing
    )
    assert database.scalar("SELECT count(*) FROM public.access_surface_credentials") == "154"
    assert (
        database.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='repo_scopes' AND column_name IN ('access_key','access_key_hash','access_key_revoked_at')"
        )
        == "0"
    )
    hashes = set(
        json.loads(
            database.scalar(
                "SELECT jsonb_agg(key_hash) FROM public.access_surface_credentials WHERE id NOT LIKE 'existing-%'"
            )
        )
    )
    assert hashes == {
        module._access_token_hash(f"cli_synthetic_release_fixture_{n}") for n in range(1, 153)
    }
