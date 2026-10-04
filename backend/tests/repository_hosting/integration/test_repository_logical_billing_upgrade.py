"""Populated logical billing Expand rollback/retry; owned PG + auth stub only."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, A, Authority, oid, update
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261004040000_expand_repository_logical_billing.sql'
NEW_TABLES = ('version_repository_billing', 'version_repository_billing_authorizations')


def test_populated_logical_billing_expansion_preserves_data_authority_and_acls():
    spec = importlib.util.spec_from_file_location('logical_billing_upgrade_pg', ROOT / 'scripts/testing/native_postgres.py')
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
        authority = Authority(pg, project)
        Authority(pg, shadow)
        pg.sql(f"UPDATE public.version_repositories SET authority='shadow' WHERE project_id={literal(shadow)}")
        old = snapshot(pg)
        tables = [*TABLES, 'organization_usage_counters', 'organization_usage_events',
                  'organization_entitlements', 'version_repository_capacity', 'version_organization_capacity']
        before = {table: rows(pg, table) for table in tables}
        oids = json.loads(pg.value("SELECT jsonb_agg(oid) FROM pg_class WHERE relnamespace='public'::regnamespace"))
        acl_query = ('SELECT jsonb_agg(jsonb_build_array(oid::regclass::text,relacl::text) ORDER BY oid) '
                     'FROM pg_class WHERE oid IN (' + ','.join(map(str, oids)) + ')')
        acl = pg.value(acl_query)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace('NOTIFY pgrst', 'SELECT 1/0;\nNOTIFY pgrst'), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {table: rows(pg, table) for table in tables} == before
        assert pg.value(acl_query) == acl
        for table in NEW_TABLES:
            assert pg.value(f"SELECT to_regclass('public.{table}') IS NULL") == 't'
        assert pg.value("SELECT to_regprocedure('public.check_version_repository_billing(text)') IS NULL") == 't'
        assert pg.value("SELECT count(*) FROM pg_trigger WHERE tgname='version_legacy_storage_reconciliation_fence'") == '0'
        pg.sql(sql)
        assert snapshot(pg) == old and {table: rows(pg, table) for table in tables} == before
        assert pg.value(acl_query) == acl
        for table in NEW_TABLES:
            assert pg.value(f'SELECT count(*) FROM public.{table}') == '0'
            assert pg.value(f"SELECT relrowsecurity FROM pg_class WHERE oid='public.{table}'::regclass") == 't'
        pg.sql(pg.publish(legacy, '2' * 40, '3' * 40, 'b' * 40))
        assert authority.apply([update(new=oid(A))])['status'] == 'committed'
        assert pg.value('SELECT count(*) FROM public.version_repository_billing') == '0'
