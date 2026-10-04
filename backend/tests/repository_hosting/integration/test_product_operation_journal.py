"""Native Product intent metadata: real PG/stored actors, not API or S3 proof."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import Authority, symbolic, update
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential

pytestmark = pytest.mark.hosting_live
DIGEST = "1" * 64


@pytest.fixture(params=["sha1", "sha256"])
def journal_actor(pg_project, request):
    pg, project = pg_project
    fmt = request.param
    authority = Authority(pg, project, object_format=fmt,
                          roots={"a" * (40 if fmt == "sha1" else 64): "commit"})
    org = pg.value(f"SELECT org_id FROM public.projects WHERE id={literal(project)}")
    user, lease = str(uuid.uuid4()), str(uuid.uuid4())
    pg.sql(f"""
        INSERT INTO auth.users(id,aud,role,email,encrypted_password,email_confirmed_at,
            raw_app_meta_data,raw_user_meta_data,created_at,updated_at)
        VALUES({literal(user)},'authenticated','authenticated',{literal(user+'@example.test')},
            '',now(),'{{}}','{{}}',now(),now());
        INSERT INTO public.org_members(id,org_id,user_id,role)
        VALUES({literal('om-'+user)},{literal(org)},{literal(user)},'member');
        INSERT INTO public.project_members(id,org_id,project_id,user_id,role)
        VALUES({literal('pm-'+user)},{literal(org)},{literal(project)},{literal(user)},'editor');
        SELECT public.acquire_project_write_lease({literal(project)},{literal(lease)},'product-test','native-product',120);
    """)
    return SimpleNamespace(pg=pg, project=project, org=org, user=user, actor="user:"+user,
                           authority=authority, lease=lease, holder="product-test", fmt=fmt)


def begin_query(a, key, digest=DIGEST):
    return "SELECT public.begin_admitted_version_product_operation(" + ",".join(map(literal, (
        a.project, a.actor, key, digest, 1, a.lease, a.holder,
    ))) + ")"


def proposal():
    return {"updates": [update(b"HEAD", symbolic(b"refs/heads/main"), symbolic(b"refs/heads/topic"))],
            "receipt_id": None, "message": "SQL fixture"}


def prepare_query(a, key, plan=None, digest=DIGEST):
    return "SELECT public.prepare_admitted_version_product_operation(" + ",".join(map(literal, (
        a.project, a.actor, key, digest, proposal() if plan is None else plan, a.lease, a.holder,
    ))) + ")"


def call(a, query):
    return json.loads(a.pg.value("SET ROLE service_role;" + query))


def count(a):
    return int(a.pg.value("SELECT count(*) FROM public.version_product_operations "
                         f"WHERE project_id={literal(a.project)}"))


def test_native_product_intent_clock_and_proposal_are_retry_stable(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    first = call(a, begin_query(a, key))
    assert first["project_id"] == a.project and first["actor"] == a.actor
    assert first["request_key"] == key and first["input_sha256"] == DIGEST
    assert first["object_format"] == a.fmt and first["generation"] == 1
    assert first["created_at"] and first["proposal"] is None and first["result"] is None
    assert call(a, begin_query(a, key)) == first
    prepared = call(a, prepare_query(a, key))
    assert prepared["created_at"] == first["created_at"]
    assert prepared["proposal"] == proposal() and prepared["result"] is None
    assert call(a, prepare_query(a, key)) == prepared
    assert call(a, begin_query(a, key)) == prepared
    assert count(a) == 1 and a.authority.count("version_ref_transactions") == 0
    assert a.authority.state(b"HEAD")["target"] == b"refs/heads/main".hex()


@pytest.mark.parametrize("stage", ["begin", "prepare"])
def test_native_product_request_identity_cannot_be_reused(journal_actor, stage):
    a, key = journal_actor, str(uuid.uuid4())
    original = call(a, begin_query(a, key))
    query = begin_query(a, key, "2"*64) if stage == "begin" else prepare_query(a, key, digest="2"*64)
    rejected = a.pg.sql("SET ROLE service_role;" + query, check=False)
    assert rejected.returncode and "request_key_reused" in rejected.stderr
    assert call(a, begin_query(a, key)) == original
    assert count(a) == 1


def test_native_product_prepared_plan_cannot_change(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    call(a, begin_query(a, key))
    original = call(a, prepare_query(a, key))
    changed = {**proposal(), "message": "different candidate"}
    rejected = a.pg.sql("SET ROLE service_role;" + prepare_query(a, key, changed), check=False)
    assert rejected.returncode and "request_key_reused" in rejected.stderr
    assert call(a, begin_query(a, key)) == original


def test_native_product_replay_is_read_only_and_bound_to_original_ref_digest(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    call(a, begin_query(a, key))
    call(a, prepare_query(a, key))
    result = a.authority.apply(proposal()["updates"], actor=a.actor, key=key, receipt=False)
    assert result["status"] == "committed"
    a.pg.sql(f"""
        UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)};
        DELETE FROM public.project_write_leases WHERE id={literal(a.lease)};
        UPDATE public.version_repositories SET generation=generation+1,write_state='fenced'
        WHERE project_id={literal(a.project)};
    """)
    a.lease = a.holder = None
    replay = call(a, begin_query(a, key))
    assert replay["result"] == result
    assert call(a, prepare_query(a, key)) == replay
    assert count(a) == 1 and a.authority.count("version_ref_transactions") == 1
    changed = a.pg.sql("SET ROLE service_role;" + begin_query(a, key, "2"*64), check=False)
    assert changed.returncode and "request_key_reused" in changed.stderr
    a.pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(a.org)} AND user_id={literal(a.user)}")
    denied = a.pg.sql("SET ROLE service_role;" + begin_query(a, key), check=False)
    assert denied.returncode and "repository_action_denied" in denied.stderr


@pytest.mark.parametrize("existing_intent", ["absent", "unprepared", "prepared"])
def test_native_product_cannot_claim_or_publish_an_unrelated_result(journal_actor, existing_intent):
    a, key = journal_actor, str(uuid.uuid4())
    if existing_intent != "absent":
        call(a, begin_query(a, key))
    if existing_intent == "prepared":
        call(a, prepare_query(a, key))
    unrelated = a.pg.sql(a.authority.query(
        [update(b"HEAD", symbolic(b"refs/heads/main"), symbolic(b"refs/heads/other"))],
        actor=a.actor, key=key, receipt=False,
    ), check=False)
    if existing_intent == "absent":
        assert unrelated.returncode == 0
        denied = a.pg.sql("SET ROLE service_role;" + begin_query(a, key), check=False)
        assert denied.returncode and "request_key_reused" in denied.stderr
        assert count(a) == 0
        assert a.authority.state(b"HEAD")["target"] == b"refs/heads/other".hex()
        assert a.authority.count("version_ref_transactions") == 1
    else:
        assert unrelated.returncode and "request_key_reused" in unrelated.stderr
        assert count(a) == 1 and call(a, begin_query(a, key))["result"] is None
        assert a.authority.state(b"HEAD")["target"] == b"refs/heads/main".hex()
        for table in ("version_ref_transactions", "version_reflog_entries", "version_ref_events", "audit_logs"):
            assert a.authority.count(table) == 0


@pytest.mark.parametrize("denial", ["viewer", "lease", "scope", "credential"])
def test_native_product_new_intent_requires_current_actor_and_lease(journal_actor, denial):
    a, key = journal_actor, str(uuid.uuid4())
    if denial == "viewer":
        a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}")
    elif denial == "lease":
        a.lease = None
    else:
        _, credential, a.actor = seed_credential(a, scope=denial == "scope")
        if denial == "credential":
            a.pg.sql(f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(credential)}")
    denied = a.pg.sql("SET ROLE service_role;" + begin_query(a, key), check=False)
    reason = "repository_write_lease_unavailable" if denial == "lease" else "repository_action_denied"
    assert denied.returncode and reason in denied.stderr
    assert count(a) == 0


@pytest.mark.parametrize("change", ["viewer", "lease", "generation", "fenced"])
def test_native_product_prepare_rechecks_current_admission(journal_actor, change):
    a, key = journal_actor, str(uuid.uuid4())
    call(a, begin_query(a, key))
    if change == "viewer":
        a.pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}")
    elif change == "lease":
        a.pg.sql(f"DELETE FROM public.project_write_leases WHERE id={literal(a.lease)}")
    else:
        assignment = "generation=generation+1" if change == "generation" else "write_state='fenced'"
        a.pg.sql(f"UPDATE public.version_repositories SET {assignment} WHERE project_id={literal(a.project)}")
    denied = a.pg.sql("SET ROLE service_role;" + prepare_query(a, key), check=False)
    expected = {"viewer": "repository_action_denied", "lease": "repository_write_lease_unavailable",
                "generation": "generation_mismatch", "fenced": "repository_unavailable"}[change]
    assert denied.returncode and expected in denied.stderr
    assert a.pg.value(f"SELECT proposal IS NULL AND native_request_sha256 IS NULL FROM public.version_product_operations WHERE project_id={literal(a.project)}") == "t"
    assert a.authority.count("version_ref_transactions") == 0


@pytest.mark.parametrize("invalid", ["missing", "extra", "empty", "duplicate", "name", "state", "receipt", "message"])
def test_native_product_malformed_proposal_cannot_poison_the_request(journal_actor, invalid):
    a, key = journal_actor, str(uuid.uuid4())
    original = call(a, begin_query(a, key))
    plan = proposal()
    if invalid == "missing":
        del plan["receipt_id"]
    elif invalid == "extra":
        plan["anything"] = "not accepted"
    elif invalid == "empty":
        plan["updates"] = []
    elif invalid == "duplicate":
        plan["updates"] *= 2
    elif invalid == "name":
        plan["updates"][0]["name_b64"] = "invalid%%"
    elif invalid == "state":
        plan["updates"][0]["expected"] = {"kind": "oid", "oid": "0" * 40}
    elif invalid == "receipt":
        plan["receipt_id"] = "not-a-uuid"
    else:
        plan["message"] = "x" * 8193
    denied = a.pg.sql("SET ROLE service_role;" + prepare_query(a, key, plan), check=False)
    assert denied.returncode and "invalid_product_proposal" in denied.stderr
    assert call(a, begin_query(a, key)) == original


@pytest.mark.parametrize("entry", ["begin", "prepare"])
@pytest.mark.parametrize("expiring", ["lease", "credential"])
def test_native_product_expiry_after_lock_wait_rolls_back_metadata(journal_actor, entry, expiring):
    a, key = journal_actor, str(uuid.uuid4())
    _, credential, a.actor = seed_credential(a)
    if entry == "prepare":
        call(a, begin_query(a, key))
    table, identity = ("project_write_leases", a.lease) if expiring == "lease" else ("access_surface_credentials", credential)
    a.pg.sql(f"UPDATE public.{table} SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(identity)}")
    name = "product-journal-expiry-" + uuid.uuid4().hex
    query = begin_query(a, key) if entry == "begin" else prepare_query(a, key)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(a.pg, f"SELECT 1 FROM public.version_repositories WHERE project_id={literal(a.project)} FOR UPDATE") as session:
            pending = pool.submit(a.pg.sql, f"SET application_name={literal(name)};SET ROLE service_role;" + query, check=False)
            wait_for_lock(a.pg, name)
            deadline = time.monotonic() + 5
            while a.pg.value(f"SELECT expires_at<=clock_timestamp() FROM public.{table} WHERE id={literal(identity)}") != "t":
                assert time.monotonic() < deadline, "expiry barrier timed out"
                time.sleep(0.02)
            session.execute("COMMIT")
        denied = pending.result(timeout=10)
    expected = "repository_write_lease_unavailable" if expiring == "lease" else "repository_action_denied"
    assert denied.returncode and expected in denied.stderr
    assert count(a) == int(entry == "prepare")
    assert a.pg.value(f"SELECT count(*) FROM public.version_product_operations WHERE project_id={literal(a.project)} AND proposal IS NOT NULL") == "0"
    assert a.authority.count("version_ref_transactions") == 0


def test_native_product_journal_private_privileges_and_cleanup_identity(journal_actor):
    a, key = journal_actor, str(uuid.uuid4())
    private = ["_version_product_ref_request_sha256(bigint,text,jsonb)",
               "_version_product_operation_result(public.version_product_operations)",
               "_version_fence_product_operation_result()", "_version_attach_product_event()"]
    exposed = ["read_admitted_version_product_operation(text,text,uuid,text,bigint)",
               "begin_admitted_version_product_operation(text,text,uuid,text,bigint,uuid,text)",
               "prepare_admitted_version_product_operation(text,text,uuid,text,jsonb,uuid,text)"]
    for signature in private + exposed:
        for role in ("anon", "authenticated", "service_role"):
            actual = a.pg.value(f"SELECT has_function_privilege({literal(role)},{literal('public.'+signature)},'EXECUTE')")
            assert actual == ("t" if role == "service_role" and signature in exposed else "f")
        config = a.pg.value(f"SELECT proconfig::text FROM pg_proc WHERE oid={literal('public.'+signature)}::regprocedure")
        assert "pg_catalog, public, pg_temp" in config
    for role in ("anon", "authenticated", "service_role"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = a.pg.value(f"SELECT has_table_privilege({literal(role)},'public.version_product_operations',{literal(privilege)})")
            assert actual == ("t" if role == "service_role" and privilege == "SELECT" else "f")
    assert a.pg.value("SELECT relrowsecurity FROM pg_class WHERE oid='public.version_product_operations'::regclass") == "t"
    call(a, begin_query(a, key))
    # Owned fixture cleanup, never service-role Project DELETE.
    a.pg.sql(f"DELETE FROM public.projects WHERE id={literal(a.project)}")
    assert count(a) == 1
    denied = a.pg.sql("SET ROLE service_role;" + begin_query(a, key), check=False)
    assert denied.returncode and "repository_unavailable" in denied.stderr
