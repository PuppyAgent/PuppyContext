"""Checked renewal Expand preserves populated pins, authority, data and old ACLs."""
import importlib.util
import uuid
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261004080000_expand_admitted_pin_renewal.sql'


def test_admitted_pin_renewal_populated_expand_rollback_retry():
    spec = importlib.util.spec_from_file_location('renewal_upgrade_pg', ROOT / 'scripts/testing/native_postgres.py')
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
        pin = str(uuid.uuid4())
        pg.sql(f"SELECT public.begin_version_repository_read({literal(project)},'job:upgrade',{literal(pin)})")
        tables = [*TABLES, 'version_object_pins', 'version_publication_admissions',
                  'version_repository_capacity_inflight', 'version_repository_object_capacity',
                  'organization_usage_counters', 'organization_usage_events']
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        acl = "SELECT COALESCE(jsonb_agg(jsonb_build_array(oid,proacl) ORDER BY oid),'[]') FROM pg_proc WHERE pronamespace='public'::regnamespace"
        old_acls = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace('NOTIFY pgrst', 'SELECT 1/0;\nNOTIFY pgrst'), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acls
        assert pg.value("SELECT to_regprocedure('public.renew_admitted_version_object_pin(text,text,uuid)') IS NULL") == 't'
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl+" AND proname<>'renew_admitted_version_object_pin'") == old_acls
        # Backend-only job protection retains its separate primitive capability.
        pg.sql(f"SET ROLE service_role; SELECT public.renew_version_object_publication({literal(project)},'job:upgrade',{literal(pin)})")
        pg.sql(pg.publish(legacy, '2'*40, '3'*40, 'b'*40))
