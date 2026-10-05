"""Function-only Expand preserves populated legacy/native facts and old RPCs."""

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.ref_authority import TABLES, Authority
from tests.repository_hosting.integration.test_native_operation_status import query
from tests.repository_hosting.integration.test_product_operation_journal import (
    begin_query,
    call,
    prepare_query,
    proposal,
)
from tests.repository_hosting.integration.test_publication_pins_upgrade import rows
from tests.repository_hosting.integration.test_ref_authority_upgrade import snapshot

pytestmark = pytest.mark.hosting_live
ROOT = Path(__file__).resolve().parents[4]
EXPAND = ROOT / "supabase/migrations/20261005030000_expand_native_operation_status.sql"


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_native_operation_status_populated_expand_rollback_and_retry(fmt):
    spec = importlib.util.spec_from_file_location("operation_status_upgrade_pg", ROOT / "scripts/testing/native_postgres.py")
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
        authority = Authority(pg, project, object_format=fmt,
                              roots={"a" * (40 if fmt == "sha1" else 64): "commit"})
        actor = "user:" + pg.value(f"SELECT created_by FROM public.projects WHERE id={literal(project)}")
        a = SimpleNamespace(pg=pg, project=project, actor=actor, lease=str(uuid.uuid4()), holder="status-upgrade")
        pg.sql(f"SELECT public.acquire_project_write_lease({literal(project)},{literal(a.lease)},'status-upgrade','status-upgrade',120)")
        committed, pending = str(uuid.uuid4()), str(uuid.uuid4())
        call(a, begin_query(a, committed))
        call(a, prepare_query(a, committed))
        result = authority.apply(proposal()["updates"], actor=actor, key=committed, receipt=False)
        call(a, begin_query(a, pending))
        tables = [*TABLES, "version_object_pins", "version_publication_admissions", "version_product_operations",
                  "version_product_publication_attempts", "version_repository_capacity_inflight",
                  "version_repository_object_capacity", "organization_usage_counters", "organization_usage_events"]
        before = {name: rows(pg, name) for name in tables}
        old = snapshot(pg)
        acl = "SELECT COALESCE(jsonb_agg(jsonb_build_array(oid,proacl) ORDER BY oid),'[]') FROM pg_proc WHERE pronamespace='public'::regnamespace"
        old_acls = pg.value(acl)
        sql = EXPAND.read_text()
        failed = pg.sql(sql.replace("NOTIFY pgrst", "SELECT 1/0;\nNOTIFY pgrst"), check=False)
        assert failed.returncode and "division by zero" in failed.stderr
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl) == old_acls
        assert pg.value("SELECT to_regprocedure('public.get_admitted_version_operation_status(text,text,uuid)') IS NULL") == "t"
        pg.sql(sql)
        assert snapshot(pg) == old and {name: rows(pg, name) for name in tables} == before
        assert pg.value(acl + " AND proname<>'get_admitted_version_operation_status'") == old_acls
        pg.sql(f"DELETE FROM public.project_write_leases WHERE id={literal(a.lease)}")
        assert call(a, query(a, committed))["result"] == result
        assert call(a, query(a, pending))["status"] == "pending"
        assert authority.apply(proposal()["updates"], actor=actor, key=committed, receipt=False) == result
        assert {name: rows(pg, name) for name in tables} == before
        pg.sql(pg.publish(legacy, "2"*40, "3"*40, "b"*40))
