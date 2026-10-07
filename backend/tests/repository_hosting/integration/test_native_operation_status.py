"""Current-reader operation lookup; metadata/SQL proof, not object publication."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import symbolic, update
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_product_operation_journal import (
    DIGEST,
    begin_query,
    call,
    prepare_query,
    proposal,
)
from tests.repository_hosting.integration.test_product_operation_journal import (
    journal_actor as journal_actor,
)
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential

pytestmark = pytest.mark.hosting_live


def query(a, key):
    return "SELECT coalesce(public.get_admitted_version_operation_status(" + ",".join(map(literal, (
        a.project, a.actor, key,
    ))) + "),'null'::jsonb)"


def footprint(a):
    tables = ("version_product_operations", "version_product_publication_attempts",
              "version_ref_transactions", "version_ref_events", "version_reflog_entries",
              "version_object_pins", "version_repository_capacity_inflight", "project_write_leases")
    return [a.pg.value(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') "
                       f"FROM public.{table} t WHERE project_id={literal(a.project)}") for table in tables]


def test_native_operation_status_missing_is_not_an_allocation(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    before = footprint(a)
    assert call(a, query(a, key)) is None
    assert footprint(a) == before


def test_native_operation_status_pending_is_not_an_ack_or_draft_read_grant(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    call(a, begin_query(a, key))
    expected = {"project_id": a.project, "actor": a.actor, "request_key": key,
                "status": "pending", "input_sha256": DIGEST,
                "ref_request_sha256": None, "result": None, "product": None}
    before = footprint(a)
    assert call(a, query(a, key)) == expected
    assert footprint(a) == before
    call(a, prepare_query(a, key))
    before = footprint(a)
    assert call(a, query(a, key)) == expected
    assert footprint(a) == before


@pytest.mark.parametrize("with_preparation", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_native_operation_status_uses_only_the_original_canonical_result(journal_actor, with_preparation, rejected):
    a, key = journal_actor, str(uuid.uuid4())
    plan = proposal()
    if rejected:
        plan["updates"] = [update(b"HEAD", symbolic(b"refs/heads/wrong"), symbolic(b"refs/heads/topic"))]
    if with_preparation:
        call(a, begin_query(a, key))
        call(a, prepare_query(a, key, plan))
    result = a.authority.apply(plan["updates"], actor=a.actor, key=key, receipt=False)
    assert result["status"] == ("rejected" if rejected else "committed")
    a.pg.sql(f"""
        UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)};
        DELETE FROM public.project_write_leases WHERE id={literal(a.lease)};
        UPDATE public.version_repositories SET generation=generation+1,write_state='fenced'
        WHERE project_id={literal(a.project)};
    """)
    before = footprint(a)
    status = call(a, query(a, key))
    assert status == {"project_id": a.project, "actor": a.actor, "request_key": key,
                      "status": result["status"], "input_sha256": DIGEST if with_preparation else None,
                      "ref_request_sha256": a.pg.value("SELECT request_sha256 FROM public.version_ref_transactions "
                                                       f"WHERE project_id={literal(a.project)} AND request_key={literal(key)}"),
                      "result": result, "product": None}
    assert footprint(a) == before
    # Same UUID belonging to a different actor is not the caller's operation.
    original_actor = a.actor
    a.actor = "user:" + a.pg.value(f"SELECT created_by FROM public.projects WHERE id={literal(a.project)}")
    assert call(a, query(a, key)) is None
    a.actor = original_actor
    a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
    denied = a.pg.sql("SET ROLE service_role;" + query(a, key), check=False)
    assert denied.returncode and "repository_action_denied" in denied.stderr


@pytest.mark.parametrize("barrier", ["preparation", "result"])
def test_native_operation_status_rechecks_expiry_after_row_wait(journal_actor, barrier):
    a, key = journal_actor, str(uuid.uuid4())
    _, credential, a.actor = seed_credential(a)
    if barrier == "preparation":
        call(a, begin_query(a, key))
        table = "version_product_operations"
    else:
        a.authority.apply(proposal()["updates"], actor=a.actor, key=key, receipt=False)
        table = "version_ref_transactions"
    a.pg.sql(f"UPDATE public.access_surface_credentials SET expires_at=clock_timestamp()+interval '2 seconds' "
             f"WHERE id={literal(credential)}")
    before = footprint(a)
    name = "operation-status-expiry-" + uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(a.pg, f"SELECT 1 FROM public.{table} WHERE project_id={literal(a.project)} FOR UPDATE") as session:
            pending = pool.submit(a.pg.sql, f"SET application_name={literal(name)};SET ROLE service_role;" + query(a, key), check=False)
            wait_for_lock(a.pg, name)
            deadline = time.monotonic() + 5
            while a.pg.value(f"SELECT expires_at<=clock_timestamp() FROM public.access_surface_credentials WHERE id={literal(credential)}") != "t":
                assert time.monotonic() < deadline, "expiry barrier timed out"
                time.sleep(0.02)
            session.execute("COMMIT")
        denied = pending.result(timeout=10)
    assert denied.returncode and "repository_action_denied" in denied.stderr
    assert footprint(a) == before


def test_native_operation_status_rpc_is_backend_only(journal_actor):
    a = journal_actor
    signature = "public.get_admitted_version_operation_status(text,text,uuid)"
    for role, allowed in (("anon", "false"), ("authenticated", "false"), ("service_role", "true")):
        assert a.pg.value(f"SELECT to_jsonb(has_function_privilege({literal(role)}, {literal(signature)}, 'EXECUTE'))") == allowed
