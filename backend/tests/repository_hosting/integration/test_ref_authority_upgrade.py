"""Product migrations and populated upgrade in an owned DB with auth stubs.

Even on a Supabase server this supplementary fixture is NOT a real Auth upgrade,
live-data migration, PostgREST acceptance, or S3 durability verification.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / "supabase/migrations/20261003010000_expand_repository_ref_authority.sql"


def snapshot(pg):
    tables = (
        "auth.users", "public.profiles", "public.organizations", "public.org_members",
        "public.projects", "public.project_members", "public.version_scope_state",
        "public.version_commits", "public.version_transactions", "public.audit_logs",
        "public.version_outbox", "public.version_refs",
    )
    result = {
        table: pg.value(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM {table} t;")
        for table in tables
    }
    result["legacy_acl"] = pg.value("""
      SELECT jsonb_agg(jsonb_build_array(oid::regprocedure::text, proacl) ORDER BY oid::regprocedure::text)
      FROM pg_proc WHERE pronamespace='public'::regnamespace
        AND proname IN ('publish_version_project_update','publish_mut_project_update',
                        'publish_version_project_update_with_usage');
    """)
    return result


def test_ref_authority_populated_expand_and_failed_ddl_rollback_with_auth_stubs():
    spec = importlib.util.spec_from_file_location(
        "hosting_native_postgres", ROOT / "scripts/testing/native_postgres.py",
    )
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
        for migration in sorted(EXPAND.parent.glob("*.sql")):
            if migration.name >= EXPAND.name:
                break
            pg.sql(native.product_migration_sql(migration))
        project = pg.create_project()
        published = json.loads(pg.value(pg.publish(project, "1" * 40, "2" * 40, "a" * 40)))
        assert published["published"] is True
        pg.sql(f"""
          INSERT INTO public.version_refs(project_id, scope_path, ref_name, ref_type, commit_id)
          VALUES ({literal(project)}, '', 'refs/heads/retained', 'branch', {literal('a' * 40)});
        """)
        before = snapshot(pg)
        body = EXPAND.read_text()
        # A late failure must roll back tables, ACLs and trigger installation.
        failure = pg.sql(body.replace("NOTIFY pgrst", "SELECT 1 / 0;\nNOTIFY pgrst"), check=False)
        assert failure.returncode != 0 and "division by zero" in failure.stderr
        assert snapshot(pg) == before
        assert pg.value("SELECT to_regclass('public.version_repositories') IS NULL;") == "t"
        assert pg.value("SELECT count(*) FROM pg_trigger WHERE tgname='zz_version_repository_root_fence';") == "0"
        assert pg.value("SELECT count(*) FROM pg_proc WHERE proname='apply_version_ref_transaction';") == "0"

        # Retry the unchanged reviewed migration; it must not rewrite any row.
        pg.sql(body)
        assert snapshot(pg) == before
        assert pg.value("SELECT count(*) FROM public.version_repositories;") == "0"
        assert pg.value("SELECT count(*) FROM public.version_repository_refs;") == "0"
        query = pg.publish(project, "2" * 40, "3" * 40, "b" * 40).replace(
            "publish_version_project_update", "publish_mut_project_update",
        )
        assert json.loads(pg.value("SET ROLE service_role;" + query))["published"] is True
        assert pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)};") == "2"
        assert pg.value(f"SELECT commit_id FROM public.version_refs WHERE project_id={literal(project)};") == "a" * 40
        assert pg.value(f"SELECT version_root_hash=mut_root_hash FROM public.projects WHERE id={literal(project)};") == "t"
