"""Populated file-admission Expand is empty, retryable and non-activating."""
import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres
from tests.repository_hosting.harness.ref_authority import TABLES, A, Authority, oid, update
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261004060000_expand_repository_file_admission.sql'
NEW_TABLES = ('version_repository_file_policies', 'version_repository_file_authorizations', 'version_publication_admissions')


def test_repository_file_policy_populated_expand_rollback_and_retry():
    spec = importlib.util.spec_from_file_location('file_policy_upgrade_pg', ROOT / 'scripts/testing/native_postgres.py')
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
        authority = Authority(pg, project)
        tables = [*TABLES, 'version_repository_billing', 'version_repository_billing_authorizations',
                  'organization_usage_counters', 'organization_usage_events', 'organization_entitlements',
                  'version_repository_capacity', 'version_repository_object_capacity', 'version_repository_capacity_inflight',
                  'version_storage_reconciliations', 'version_storage_reconciliation_projects']
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        acl = "SELECT COALESCE(jsonb_agg(jsonb_build_array(oid,proacl) ORDER BY oid),'[]') FROM pg_proc WHERE pronamespace='public'::regnamespace"
        old_acls = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace('NOTIFY pgrst', 'SELECT 1/0;\nNOTIFY pgrst'), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acls
        for name in NEW_TABLES:
            assert pg.value(f"SELECT to_regclass('public.{name}') IS NULL") == 't'
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        # Compare ACLs for the original OIDs; added functions have separate ACL tests.
        import json
        original_oids = ','.join(str(row[0]) for row in json.loads(old_acls))
        assert pg.value(acl+f' AND oid IN ({original_oids})') == old_acls
        for name in NEW_TABLES:
            assert pg.value(f'SELECT count(*) FROM public.{name}') == '0'
            assert pg.value(f"SELECT relrowsecurity FROM pg_class WHERE oid='public.{name}'::regclass") == 't'
        pg.sql(pg.publish(legacy, '2'*40, '3'*40, 'b'*40))
        assert authority.apply([update(new=oid(A))])['status'] == 'committed'
        assert pg.value('SELECT count(*) FROM public.version_repository_file_policies') == '0'
