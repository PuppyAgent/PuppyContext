"""Immutable forward SQL repair: populated upgrade, rollback, retry and ACLs."""

import importlib.util
import json
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
REPAIR = ROOT / "supabase/migrations/20261003030000_fix_legacy_publication_head_cas.sql"


def function_contract(pg):
    return pg.value("""
      SELECT jsonb_agg(jsonb_build_array(oid, proowner, proacl, prosecdef,
        proargtypes::text, proallargtypes::text, proargmodes::text, proargnames::text,
        pg_get_expr(proargdefaults, 0), prorettype, proretset) ORDER BY oid)
      FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname IN (
        'publish_version_project_update', 'publish_mut_project_update',
        'publish_version_project_update_with_usage');
    """)


def test_populated_head_cas_upgrade_preserves_contract_and_failed_ddl_retry():
    spec = importlib.util.spec_from_file_location("head_cas_native_pg", ROOT / "scripts/testing/native_postgres.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
        for migration in sorted(REPAIR.parent.glob("*.sql")):
            if migration.name >= REPAIR.name:
                break
            pg.sql(native.product_migration_sql(migration))
        project = pg.create_project()
        assert json.loads(pg.value(pg.publish(project, "1" * 40, "1" * 40, "a" * 40)))["published"]
        pg.sql(f"""
          INSERT INTO public.version_refs(project_id,scope_path,ref_name,ref_type,commit_id)
          VALUES ({literal(project)}, '', 'refs/heads/retained', 'branch', {literal('a' * 40)});
        """)
        before, contract = snapshot(pg), function_contract(pg)
        definition_query = "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname='publish_version_project_update';"
        definition = pg.value(definition_query)
        sql = REPAIR.read_text()
        failed = pg.sql(sql.replace("NOTIFY pgrst", "SELECT 1 / 0;\nNOTIFY pgrst"), check=False)
        assert failed.returncode != 0 and "division by zero" in failed.stderr
        assert snapshot(pg) == before
        assert function_contract(pg) == contract
        assert pg.value(definition_query) == definition
        pg.sql(sql)
        pg.sql(sql)
        assert snapshot(pg) == before
        assert function_contract(pg) == contract
        assert pg.value(definition_query) != definition
        stale = pg.publish(project, "1" * 40, "1" * 40, "b" * 40, expected_head="")
        assert not json.loads(pg.value("SET ROLE service_role;" + stale))["published"]
        assert snapshot(pg) == before
        # The retained compatibility RPC must still work after the repair.
        legacy = pg.publish(project, "1" * 40, "2" * 40, "c" * 40).replace(
            "publish_version_project_update", "publish_mut_project_update",
        )
        assert json.loads(pg.value("SET ROLE service_role;" + legacy))["published"]
        for role in ("anon", "authenticated"):
            rejected = pg.sql(f"SET ROLE {role};" + legacy, check=False)
            assert rejected.returncode != 0 and "permission denied" in rejected.stderr
        assert pg.value(f"SELECT commit_id FROM public.version_refs WHERE project_id={literal(project)}") == "a" * 40
