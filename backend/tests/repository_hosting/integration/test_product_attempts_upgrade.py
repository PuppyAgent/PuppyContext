"""Populated forward expansion preserves old Product results and uncertain inventory."""
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES
from tests.repository_hosting.integration.test_product_operation_journal import (
    begin_query,
    call,
    journal_actor,
    prepare_query,
    proposal,
)
from tests.repository_hosting.integration.test_product_publication_attempts import (
    open_query,
    prepared_attempt,
)
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / 'supabase/migrations/20261005020000_expand_product_publication_attempts.sql'


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_product_attempts_populated_expand_rollback_and_old_result_replay(object_format):
    spec = importlib.util.spec_from_file_location('product_attempts_upgrade_pg', ROOT / 'scripts/testing/native_postgres.py')
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
        a = journal_actor.__wrapped__((pg, project), SimpleNamespace(param=object_format))
        committed = str(uuid.uuid4())
        call(a, begin_query(a, committed))
        call(a, prepare_query(a, committed))
        result = a.authority.apply(proposal()['updates'], key=committed, actor=a.actor, receipt=False)
        _, pending_key, original = prepared_attempt.__wrapped__(a)
        pin = original['proposal']['receipt_id']
        oid = original['proposal']['product_result']['commit_oid']
        call(a, 'SELECT public.begin_admitted_version_object_publication(' + ','.join(map(literal, (
            project, a.actor, pin, 1, {oid: 'commit'}, a.lease, a.holder,
        ))) + ')')
        pg.sql(f"INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects) "
               f"VALUES({literal(a.org)},true,100,10); "
               f"INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects) "
               f"VALUES({literal(project)},{literal(a.org)},true,100,10)")
        call(a, 'SELECT public.reserve_version_object_capacity(' + ','.join(map(literal, (
            project, a.actor, pin, [{'object_id': oid, 'object_kind': 'commit', 'body_bytes': 6}],
        ))) + ')')
        tables = [*TABLES, 'version_object_pins', 'version_publication_admissions', 'version_product_operations',
                  'version_repository_capacity_inflight', 'version_repository_object_capacity',
                  'organization_usage_counters', 'organization_usage_events']
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        acl = "SELECT coalesce(jsonb_agg(jsonb_build_array(oid,proacl) ORDER BY oid),'[]') FROM pg_proc WHERE pronamespace='public'::regnamespace"
        old_acls = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace("NOTIFY pgrst", "SELECT 1/0;\nNOTIFY pgrst"), check=False)
        assert failed.returncode and 'division by zero' in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acls
        assert pg.value("SELECT to_regclass('public.version_product_publication_attempts') IS NULL") == 't'
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        added = ['_version_fence_product_attempt_pin', '_version_fence_product_attempt_admission', 'open_admitted_version_product_attempt']
        assert pg.value(acl + ' AND proname NOT IN (' + ','.join(map(literal, added)) + ')') == old_acls
        assert pg.value('SELECT count(*) FROM public.version_product_publication_attempts') == '0'
        pg.sql(f"DELETE FROM public.project_write_leases WHERE id={literal(a.lease)}; "
               f"UPDATE public.project_members SET role='viewer' WHERE project_id={literal(project)} AND user_id={literal(a.user)}")
        assert call(a, begin_query(a, committed))['result'] == result
        a.lease, a.holder = str(uuid.uuid4()), 'after-upgrade'
        pg.sql(f"UPDATE public.project_members SET role='editor' WHERE project_id={literal(project)} AND user_id={literal(a.user)}; "
               'SELECT public.acquire_project_write_lease(' + ','.join(map(literal, (
                   project, a.lease, a.holder, 'after-upgrade', 120,
               ))) + ')')
        resumed = call(a, open_query(a, pending_key, str(uuid.uuid4())))
        assert resumed['proposal']['receipt_id'] != pin
        assert rows(pg, 'version_repository_capacity_inflight') == before['version_repository_capacity_inflight']
        assert rows(pg, 'version_product_operations') == before['version_product_operations']
        pg.sql(pg.publish(legacy, '2'*40, '3'*40, 'b'*40))
