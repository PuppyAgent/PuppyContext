"""Populated forward expansion/rollback, not storage or live-user migration proof."""
import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_object_location_fence import insert, location
from tests.repository_hosting.integration.test_publication_pins import begin
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / "supabase/migrations/20261004010000_expand_object_location_publication_fence.sql"


def test_populated_location_fence_rolls_back_and_retries_without_data_or_acl_rewrite():
    spec = importlib.util.spec_from_file_location("locations_native_pg", ROOT / "scripts/testing/native_postgres.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
        for migration in sorted(EXPAND.parent.glob("*.sql")):
            if migration.name >= EXPAND.name:
                break
            pg.sql(native.product_migration_sql(migration))
        legacy, project, shadow = (pg.create_project() for _ in range(3))
        pg.sql(pg.publish(legacy, "1" * 40, "2" * 40, "a" * 40))
        auth = Authority(pg, project)
        begin(auth)
        Authority(pg, shadow)
        pg.sql(f"UPDATE public.version_repositories SET authority='shadow' WHERE project_id={literal(shadow)}")
        for target in (legacy, project, shadow):
            pg.sql(insert(location(target)))
        old = snapshot(pg)
        tables = [*TABLES, "version_object_locations", "version_object_pins",
                  "version_repository_gc_runs", "version_repository_root_metadata"]
        before = {table: rows(pg, table) for table in tables}
        acl_query = "SELECT relacl::text FROM pg_class WHERE oid='public.version_object_locations'::regclass"
        acl = pg.value(acl_query)
        rpc_query = "SELECT to_regprocedure('public.register_version_object_locations(text,text,uuid,jsonb)') IS NULL"
        sql = EXPAND.read_text()
        failure = pg.sql(sql.replace("NOTIFY pgrst", "SELECT 1/0;\nNOTIFY pgrst"), check=False)
        assert failure.returncode and "division by zero" in failure.stderr
        assert snapshot(pg) == old and {table: rows(pg, table) for table in tables} == before
        assert pg.value(acl_query) == acl and pg.value(rpc_query) == "t"
        assert pg.value("SELECT count(*) FROM pg_trigger WHERE tgname='zz_version_object_location_fence'") == "0"
        pg.sql(sql)
        assert snapshot(pg) == old and {table: rows(pg, table) for table in tables} == before
        assert pg.value(acl_query) == acl and pg.value(rpc_query) == "f"
        assert pg.value("SELECT count(*) FROM pg_trigger WHERE tgname='zz_version_object_location_fence'") == "1"
        assert pg.value(f"SELECT authority FROM public.version_repositories WHERE project_id={literal(project)}") == "native"
        assert pg.value(f"SELECT authority FROM public.version_repositories WHERE project_id={literal(shadow)}") == "shadow"
        assert pg.value(f"SELECT count(*) FROM public.version_repositories WHERE project_id={literal(legacy)}") == "0"
        blocked = pg.sql(f"SET ROLE service_role; UPDATE public.version_object_locations SET offset_bytes=1 "
                         f"WHERE project_id={literal(project)}", check=False)
        assert blocked.returncode and "native_object_location_coordination_required" in blocked.stderr
        for target in (legacy, shadow):
            pg.sql(f"SET ROLE service_role; UPDATE public.version_object_locations SET offset_bytes=1 "
                   f"WHERE project_id={literal(target)}")
