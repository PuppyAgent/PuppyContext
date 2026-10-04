"""Populated forward admission expansion with rollback/retry; no data cutover."""
import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261004020000_expand_repository_write_admission.sql'


def test_populated_write_admission_expansion_preserves_rows_acl_and_authority():
    spec = importlib.util.spec_from_file_location('admission_native_pg', ROOT / 'scripts/testing/native_postgres.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / 'scripts/testing/postgres_auth_stub.sql').read_text())
        for migration in sorted(EXPAND.parent.glob('*.sql')):
            if migration.name >= EXPAND.name:
                break
            pg.sql(native.product_migration_sql(migration))
        legacy, project, shadow = (pg.create_project() for _ in range(3))
        pg.sql(pg.publish(legacy, '1' * 40, '2' * 40, 'a' * 40))
        Authority(pg, project)
        Authority(pg, shadow)
        pg.sql(f"UPDATE public.version_repositories SET authority='shadow' WHERE project_id={literal(shadow)}")
        old = snapshot(pg)
        tables = [*TABLES, 'project_members', 'org_members', 'access_surfaces',
                  'access_surface_credentials', 'project_write_leases', 'version_object_locations',
                  'version_object_pins', 'version_repository_gc_runs', 'version_repository_root_metadata']
        before = {table: rows(pg, table) for table in tables}
        acl_query = "SELECT jsonb_agg(jsonb_build_array(oid::regclass::text,relacl::text) ORDER BY oid) FROM pg_class WHERE relnamespace='public'::regnamespace"
        acl = pg.value(acl_query)
        function = "SELECT to_regprocedure('public.apply_admitted_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text)') IS NULL"
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace('NOTIFY pgrst', 'SELECT 1/0;\nNOTIFY pgrst'), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {table: rows(pg, table) for table in tables} == before
        assert pg.value(acl_query) == acl and pg.value(function) == 't'
        pg.sql(sql)
        assert snapshot(pg) == old and {table: rows(pg, table) for table in tables} == before
        assert pg.value(acl_query) == acl and pg.value(function) == 'f'
        assert pg.value(f"SELECT authority FROM public.version_repositories WHERE project_id={literal(project)}") == 'native'
        assert pg.value(f"SELECT authority FROM public.version_repositories WHERE project_id={literal(shadow)}") == 'shadow'
        assert pg.value(f"SELECT count(*) FROM public.version_repositories WHERE project_id={literal(legacy)}") == '0'
