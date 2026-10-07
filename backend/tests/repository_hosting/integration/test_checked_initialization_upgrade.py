"""Root initialization expansion performs no implicit data repair or activation."""
import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261004070000_expand_checked_root_initialization.sql'


def test_checked_root_initialization_populated_expand_rollback_and_retry():
    spec = importlib.util.spec_from_file_location('root_init_upgrade_pg', ROOT / 'scripts/testing/native_postgres.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / 'scripts/testing/postgres_auth_stub.sql').read_text())
        for migration in sorted(EXPAND.parent.glob('*.sql')):
            if migration.name >= EXPAND.name:
                break
            pg.sql(native.product_migration_sql(migration))
        legacy, project = pg.create_project(), pg.create_project()
        pg.sql(pg.publish(legacy, '1'*40, '2'*40, 'a'*40))
        Authority(pg, project)
        tables = [*TABLES, 'version_object_pins', 'version_repository_billing', 'version_repository_file_policies',
                  'version_publication_admissions', 'organization_usage_counters', 'organization_usage_events']
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        acl = "SELECT COALESCE(jsonb_agg(jsonb_build_array(oid,proacl) ORDER BY oid),'[]') FROM pg_proc WHERE pronamespace='public'::regnamespace"
        old_acls = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace('NOTIFY pgrst', 'SELECT 1/0;\nNOTIFY pgrst'), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acls
        assert pg.value("SELECT to_regprocedure('public.initialize_legacy_version_project_root(text,uuid,text)') IS NULL") == 't'
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl+" AND proname<>'initialize_legacy_version_project_root'") == old_acls
        pg.sql(pg.publish(legacy, '2'*40, '3'*40, 'b'*40))
