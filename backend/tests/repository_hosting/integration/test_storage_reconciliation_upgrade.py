"""Populated reconciliation Expand preserves data and rolls its old fence back."""
import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261004050000_expand_repository_usage_reconciliation.sql'
NEW_TABLES = ('version_storage_reconciliations', 'version_storage_reconciliation_projects')


def test_storage_reconciliation_populated_upgrade_and_rollback():
    spec = importlib.util.spec_from_file_location('usage_upgrade_pg', ROOT / 'scripts/testing/native_postgres.py')
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
        pg.sql(f"INSERT INTO public.version_repository_billing(project_id,org_id) "
               f"SELECT id,org_id FROM public.projects WHERE id={literal(project)}")
        tables = [*TABLES, 'version_repository_billing', 'version_repository_billing_authorizations',
                  'organization_usage_counters', 'organization_usage_events', 'organization_entitlements']
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        fence = "SELECT pg_get_functiondef('public._version_fence_legacy_storage_reconciliation()'::regprocedure)"
        old_fence = pg.value(fence)
        acl = "SELECT proacl::text FROM pg_proc WHERE oid='public._version_fence_legacy_storage_reconciliation()'::regprocedure"
        old_acl = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace('NOTIFY pgrst', 'SELECT 1/0;\nNOTIFY pgrst'), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(fence) == old_fence and pg.value(acl) == old_acl
        for name in NEW_TABLES:
            assert pg.value(f"SELECT to_regclass('public.{name}') IS NULL") == 't'
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acl
        for name in NEW_TABLES:
            assert pg.value(f'SELECT count(*) FROM public.{name}') == '0'
            assert pg.value(f"SELECT relrowsecurity FROM pg_class WHERE oid='public.{name}'::regclass") == 't'
        pg.sql(pg.publish(legacy, '2'*40, '3'*40, 'b'*40))
        org = pg.value(f'SELECT org_id FROM public.projects WHERE id={literal(project)}')
        denied = pg.sql('SET ROLE service_role; SELECT public.reconcile_organization_usage_counter(' +
                        ','.join(map(literal, (org, 'storage.logical_bytes', 0, 10, 'old-issuer-request',
                                              'storage_reconciler', {}))) + ')', check=False)
        assert denied.returncode and 'native_storage_reconciliation_required' in denied.stderr
