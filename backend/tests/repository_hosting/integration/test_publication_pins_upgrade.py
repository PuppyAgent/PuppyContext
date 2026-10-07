"""Populated Expand rollback/retry; synthetic rows are not object evidence."""

import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / "supabase/migrations/20261003040000_expand_publication_pins_and_gc_fence.sql"


def rows(pg, table, *, omit_token=False):
    row = "to_jsonb(t)-'gc_token'" if omit_token else "to_jsonb(t)"
    return pg.value(f"SELECT coalesce(jsonb_agg({row} ORDER BY {row}::text),'[]') FROM public.{table} t")


def test_populated_pin_expansion_rolls_back_and_retries_without_activation_or_data_rewrite():
    spec = importlib.util.spec_from_file_location("pins_native_pg", ROOT / "scripts/testing/native_postgres.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
        for migration in sorted(EXPAND.parent.glob("*.sql")):
            if migration.name >= EXPAND.name:
                break
            pg.sql(native.product_migration_sql(migration))
        project = pg.create_project()
        pg.sql(pg.publish(project, "1" * 40, "2" * 40, "a" * 40))
        Authority(pg, project)
        pg.sql(f"UPDATE public.version_repositories SET authority='shadow' WHERE project_id={literal(project)}")
        pg.sql(f"INSERT INTO public.version_object_gc_candidates(project_id,object_id,first_seen_at,last_seen_at) VALUES({literal(project)},{literal('b' * 40)},now(),now())")
        old = snapshot(pg)
        tables = [*TABLES, "version_object_gc_candidates"]
        before = {table: rows(pg, table) for table in tables}
        constraint_query = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='version_object_gc_candidates_object_id_check'"
        constraint = pg.value(constraint_query)
        sql = EXPAND.read_text()
        failure = pg.sql(sql.replace("NOTIFY pgrst", "SELECT 1/0;\nNOTIFY pgrst"), check=False)
        assert failure.returncode != 0 and "division by zero" in failure.stderr
        assert snapshot(pg) == old
        assert {table: rows(pg, table) for table in tables} == before
        assert pg.value(constraint_query) == constraint
        assert pg.value("SELECT to_regclass('public.version_object_pins') IS NULL") == "t"
        pg.sql(sql)
        assert snapshot(pg) == old
        assert {table: rows(pg, table, omit_token=table == "version_repositories") for table in tables} == before
        assert pg.value("SELECT authority FROM public.version_repositories") == "shadow"
        for table in ("version_object_pins", "version_repository_gc_runs", "version_repository_root_metadata"):
            assert pg.value(f"SELECT count(*) FROM public.{table}") == "0"
        assert pg.value(constraint_query) != constraint
        # Widening retains the old RPC and SHA-1 quarantine records; SHA-256
        # candidates can now use the same reconciliation/age contract.
        pg.sql(f"SET ROLE service_role; SELECT * FROM public.sync_version_object_gc_candidates({literal(project)},ARRAY[{literal('b' * 40)},{literal('c' * 64)}],now(),3600)")
        assert pg.value("SELECT count(*) FROM public.version_object_gc_candidates") == "2"
