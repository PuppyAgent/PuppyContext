"""Empty journal expansion cannot rewrite prior refs, data, usage or ACLs."""

import importlib.util
import uuid
from pathlib import Path

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority, symbolic, update
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / "supabase/migrations/20261005010000_expand_product_operation_journal.sql"


def test_product_operation_journal_populated_expand_rollback_retry():
    spec = importlib.util.spec_from_file_location("product_journal_upgrade_pg", ROOT / "scripts/testing/native_postgres.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with Postgres().empty_database() as pg:
        pg.sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
        for migration in sorted(EXPAND.parent.glob("*.sql")):
            if migration.name >= EXPAND.name:
                break
            pg.sql(native.product_migration_sql(migration))
        legacy, project = pg.create_project(), pg.create_project()
        pg.sql(pg.publish(legacy, "1"*40, "2"*40, "a"*40))
        authority = Authority(pg, project)
        edits = [update(b"HEAD", symbolic(b"refs/heads/main"), symbolic(b"refs/heads/topic"))]
        key = str(uuid.uuid4())
        result = authority.apply(edits, key=key, receipt=False)
        tables = [*TABLES, "version_object_pins", "version_publication_admissions",
                  "version_repository_capacity_inflight", "version_repository_object_capacity",
                  "organization_usage_counters", "organization_usage_events"]
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        acl = "SELECT COALESCE(jsonb_agg(jsonb_build_array(oid,proacl) ORDER BY oid),'[]') FROM pg_proc WHERE pronamespace='public'::regnamespace"
        old_acls = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace("NOTIFY pgrst", "SELECT 1/0;\nNOTIFY pgrst"), check=False)
        assert failed.returncode and "division by zero" in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acls
        assert pg.value("SELECT to_regclass('public.version_product_operations') IS NULL") == "t"
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        added = ["_version_product_ref_request_sha256", "_version_product_operation_result",
                 "_version_fence_product_operation_result", "begin_admitted_version_product_operation",
                 "prepare_admitted_version_product_operation", "read_admitted_version_product_operation",
                 "_version_attach_product_event"]
        assert pg.value(acl + " AND proname NOT IN (" + ",".join(map(literal, added)) + ")") == old_acls
        assert pg.value("SELECT count(*) FROM public.version_product_operations") == "0"
        assert authority.apply(edits, key=key, receipt=False) == result
        pg.sql(pg.publish(legacy, "2"*40, "3"*40, "b"*40))
