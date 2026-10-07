"""M02/M04 SQL primitive, NOT runtime cutover or object durability evidence.

Owner-installed synthetic receipts stand in for the not-yet-enabled storage
verifier. Every publication/query below executes as the real service_role.
"""

import base64
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import (
    ABSENT,
    TABLES,
    A,
    Authority,
    B,
    C,
    b64,
    oid,
    symbolic,
    update,
)

pytestmark = pytest.mark.hosting_live


@pytest.fixture
def authority(pg_project):
    return Authority(*pg_project)


@pytest.mark.parametrize("signature", [
    "apply_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text)",
    "get_version_ref_transaction(text,text,uuid)",
    "_version_fence_legacy_publication()",
])
def test_ref_authority_definer_path_matches_security_contract(pg_project, signature):
    pg, _ = pg_project
    assert pg.value(f"""
      SELECT prosecdef AND proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp']
      FROM pg_proc WHERE oid={literal('public.' + signature)}::regprocedure;
    """) == "t"


def test_ref_authority_same_tree_heads_use_oid_cas(authority):
    a = authority
    assert a.apply([update(new=oid(A))])["status"] == "committed"
    assert a.apply([update(old=oid(A), new=oid(B))])["status"] == "committed"
    stale = a.apply([update(old=oid(A), new=oid(C))])
    assert stale["status"] == "rejected"
    assert stale["reason"] == "stale_ref"
    assert stale["refs"][0]["observed"] == oid(B)
    assert a.state()["oid"] == B
    assert a.count("version_reflog_entries") == a.count("version_ref_events") == 2


def test_ref_authority_concurrent_absent_create_has_one_winner(authority):
    barrier = Barrier(8)

    def publish(i):
        barrier.wait(timeout=10)
        return authority.apply([update(new=oid(A if i % 2 else B))])

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(publish, range(8)))
    assert sum(row["status"] == "committed" for row in outcomes) == 1
    assert authority.count("version_ref_events") == 1
    assert authority.count("version_ref_transactions") == 8


def test_ref_authority_atomic_failure_is_not_partial(authority):
    a = authority
    a.apply([update(new=oid(A))])
    result = a.apply([
        update(old=oid(B), new=oid(C)),
        update(b"refs/tags/release", new=oid("e" * 40)),
    ])
    assert result["status"] == "rejected"
    assert len(result["refs"]) == 2
    assert a.state()["oid"] == A
    assert a.state(b"refs/tags/release") is None
    assert a.count("version_ref_events") == 1


def test_ref_authority_head_detach_unborn_verify_and_delete(authority):
    a = authority
    result = a.apply([
        update(new=oid(A)),
        update(b"HEAD", symbolic(b"refs/heads/main"), oid(A)),
        update(b"refs/notes/commits", new=oid(B)),
        update(b"refs/tags/blob", new=oid("d" * 40)),
    ])
    assert result["status"] == "committed"
    assert a.state(b"HEAD")["oid"] == A
    assert a.apply([update(old=oid(A))], receipt=False)["status"] == "committed"
    assert a.count("version_ref_events") == 1  # verify is not a ref change
    stale = a.apply([update(b"HEAD", symbolic(b"refs/heads/main"), oid(B))])
    assert stale["status"] == "rejected"
    assert a.apply([
        update(old=oid(A), new=ABSENT),
        update(b"HEAD", oid(A), symbolic(b"refs/heads/unborn")),
    ], receipt=False)["status"] == "committed"
    assert a.state() is None
    assert a.state(b"HEAD")["target"] == b"refs/heads/unborn".hex()
    assert a.count("version_reflog_entries") == 6


def test_ref_authority_non_utf8_ref_name_roundtrips(authority):
    name = b"refs/heads/raw-\xff"
    result = authority.apply([update(name, new=oid(A))])
    assert base64.b64decode(result["refs"][0]["name_b64"]) == name
    assert authority.state(name)["oid"] == A


def test_ref_authority_retry_and_result_query_after_expiry(authority):
    a, key = authority, str(uuid.uuid4())
    query = a.query([update(new=oid(A))], key=key)
    first = json.loads(a.pg.value(query))
    a.pg.sql(f"""
      UPDATE public.version_publication_receipts
      SET verified_at=now()-interval '2 hours', expires_at=now()-interval '1 hour'
      WHERE id={literal(a.receipt)};
      UPDATE public.version_repositories SET write_state='fenced'
      WHERE project_id={literal(a.project)};
    """)
    assert json.loads(a.pg.value(query)) == first
    found = json.loads(a.pg.value(
        "SET ROLE service_role; SELECT public.get_version_ref_transaction("
        + ",".join(map(literal, [a.project, "test:writer", key])) + ");"
    ))
    assert found == first
    assert a.count("version_ref_events") == a.count("audit_logs") == 1
    different = a.pg.sql(a.query([update(new=oid(B))], key=key), check=False)
    assert different.returncode != 0 and "request_key_reused" in different.stderr
    assert a.pg.value(
        "SET ROLE service_role; SELECT public.get_version_ref_transaction("
        + ",".join(map(literal, [a.project, "other:actor", key])) + ") IS NULL;"
    ) == "t"


def test_ref_authority_duplicate_concurrent_request_has_one_effect(authority):
    key, barrier = str(uuid.uuid4()), Barrier(4)

    def publish(_):
        barrier.wait(timeout=10)
        return authority.apply([update(new=oid(A))], key=key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(publish, range(4)))
    assert all(result == results[0] for result in results)
    assert authority.count("version_ref_transactions") == 1
    assert authority.count("version_reflog_entries") == 1


@pytest.mark.parametrize("change,error", [
    ("UPDATE public.version_repositories SET generation=2", "generation_mismatch"),
    ("UPDATE public.version_repositories SET gc_epoch=2", "invalid_receipt"),
    ("UPDATE public.version_repositories SET write_state='fenced'", "repository_unavailable"),
    ("UPDATE public.version_repositories SET authority='shadow'", "repository_unavailable"),
    ("UPDATE public.projects SET lifecycle_status='deleting'", "repository_unavailable"),
    ("UPDATE public.version_publication_receipts SET verified_at=now()-interval '2 hours', expires_at=now()-interval '1 hour'", "invalid_receipt"),
])
def test_ref_authority_admission_and_receipt_fences(authority, change, error):
    a = authority
    key_column = "id" if change.startswith("UPDATE public.projects ") else "project_id"
    a.pg.sql(change + f" WHERE {key_column}={literal(a.project)};")
    result = a.pg.sql(a.query([update(new=oid(A))]), check=False)
    assert result.returncode != 0 and error in result.stderr
    assert a.state() is None
    assert a.count("version_ref_events") == a.count("version_ref_transactions") == 0


@pytest.mark.parametrize("new,receipt,error", [
    (oid(A), False, "invalid_receipt"),
    (oid("1" * 40), True, "unverified_target"),
    (oid("d" * 40), True, "invalid_target_type"),
    (oid("e" * 40), True, "invalid_target_type"),
    (oid("a" * 64), True, "invalid_ref_state"),
    (oid("0" * 40), True, "invalid_ref_state"),
])
def test_ref_authority_rejects_unverified_wrong_kind_or_format(authority, new, receipt, error):
    result = authority.pg.sql(authority.query([update(new=new)], receipt=receipt), check=False)
    assert result.returncode != 0 and error in result.stderr
    assert authority.count("version_ref_transactions") == 0


@pytest.mark.parametrize("name", [
    b"main", b"refs/heads/../x", b"refs/heads/.hidden", b"refs/heads/x.lock",
    b"refs/heads/has space", b"refs/heads/x@{1}", b"refs/heads/x\\y",
    b"refs/heads/a//b", b"refs/heads/x\x00", b"refs/heads/x.", b"refs/heads/x\x7f",
])
def test_ref_authority_invalid_ref_names(authority, name):
    result = authority.pg.sql(authority.query([update(name, new=oid(A))]), check=False)
    assert result.returncode != 0 and "invalid_ref_name" in result.stderr
    assert authority.count("version_ref_transactions") == 0


def test_ref_authority_duplicate_and_namespace_conflicts(authority):
    a = authority
    result = a.pg.sql(a.query([update(new=oid(A)), update(new=oid(B))]), check=False)
    assert result.returncode != 0 and "duplicate_ref" in result.stderr
    result = a.apply([update(new=oid(A)), update(b"refs/heads/main/sub", new=oid(B))])
    assert result["status"] == "rejected" and result["reason"] == "ref_namespace_conflict"
    assert a.state() is None
    a.apply([update(new=oid(A))])
    assert a.apply([update(b"refs/heads/main/sub", new=oid(B))])["status"] == "rejected"
    # Final namespace, not transient update order, determines validity.
    assert a.apply([update(b"refs/heads/main/sub", new=oid(B)), update(old=oid(A), new=ABSENT)])["status"] == "committed"
    assert a.state() is None and a.state(b"refs/heads/main/sub")["oid"] == B


def test_ref_authority_outer_rollback_restores_all_publication_effects(authority):
    a = authority
    a.pg.sql("BEGIN;" + a.query([update(new=oid(A))]) + "ROLLBACK;")
    assert a.state() is None
    for table in ("version_ref_transactions", "version_ref_events", "version_reflog_entries", "audit_logs"):
        assert a.count(table) == 0
    assert a.pg.value(f"SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(a.project)}") == "0"


def test_ref_authority_outbox_failure_rolls_back_refs_and_audit(authority):
    a = authority
    # Failure occurs after ref/log/audit statements have run.
    sql = f"""
      BEGIN;
      ALTER TABLE public.version_ref_events ADD CONSTRAINT fail_fixture
        CHECK (project_id <> {literal(a.project)}) NOT VALID;
      {a.query([update(new=oid(A))])}
      COMMIT;
    """
    result = a.pg.sql(sql, check=False)
    assert result.returncode != 0 and "fail_fixture" in result.stderr
    assert a.state() is None
    for table in ("version_ref_transactions", "version_ref_events", "version_reflog_entries", "audit_logs"):
        assert a.count(table) == 0


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_ref_authority_data_api_roles_are_denied(authority, role):
    a = authority
    for table in TABLES:
        result = a.pg.sql(f"SET ROLE {role}; SELECT * FROM public.{table};", check=False)
        assert result.returncode != 0 and "permission denied" in result.stderr
    result = a.pg.sql(a.query([update(new=oid(A))]).replace("service_role", role), check=False)
    assert result.returncode != 0 and "permission denied" in result.stderr
    result = a.pg.sql(f"SET ROLE {role}; SELECT public.get_version_ref_transaction({literal(a.project)},'test:writer',{literal(str(uuid.uuid4()))});", check=False)
    assert result.returncode != 0 and "permission denied" in result.stderr


def test_ref_authority_backend_cannot_bypass_rpc_or_activate(authority):
    a = authority
    for table in TABLES:
        result = a.pg.sql(f"SET ROLE service_role; DELETE FROM public.{table} WHERE project_id={literal(a.project)};", check=False)
        assert result.returncode != 0 and "permission denied" in result.stderr
    assert a.pg.value("SELECT bool_and(relrowsecurity) FROM pg_class WHERE oid=ANY(ARRAY[" + ",".join(literal("public." + table) + "::regclass" for table in TABLES) + "]);") == "t"


@pytest.mark.parametrize("legacy", [False, True])
def test_ref_authority_legacy_publication_is_fenced(authority, legacy):
    a = authority
    query = a.pg.publish(a.project, "1" * 40, "1" * 40, A)
    if legacy:
        query = query.replace("publish_version_project_update", "publish_mut_project_update")
    result = a.pg.sql("SET ROLE service_role;" + query, check=False)
    assert result.returncode != 0 and "legacy_repository_publication_fenced" in result.stderr
    assert a.count("version_ref_events") == 0
    assert a.pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(a.project)}") == "0"


@pytest.mark.parametrize("column", ["version_root_hash", "mut_root_hash"])
def test_ref_authority_legacy_direct_root_write_is_fenced(authority, column):
    a = authority
    result = a.pg.sql(f"SET ROLE service_role; UPDATE public.projects SET {column}={literal(B)} WHERE id={literal(a.project)};", check=False)
    assert result.returncode != 0 and "legacy_repository_publication_fenced" in result.stderr


def test_ref_authority_shadow_does_not_change_legacy_contract(pg_project):
    pg, project = pg_project
    pg.sql(f"INSERT INTO public.version_repositories(project_id) VALUES ({literal(project)});")
    result = json.loads(pg.value("SET ROLE service_role;" + pg.publish(project, "1" * 40, B, A)))
    assert result["published"] is True
    assert pg.value(f"SELECT count(*) FROM public.version_repository_refs WHERE project_id={literal(project)}") == "0"


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_ref_authority_git_oracle_same_tree_cas_and_atomicity(pg_project, tmp_path, object_format):
    from tests.repository_hosting.harness.git import Git

    git = Git.init(tmp_path / "oracle", bare=True, format=object_format)
    tree = git.run("hash-object", "-w", "-t", "tree", "--stdin", input=b"").stdout.decode().strip()
    first = git.text("commit-tree", tree, "-m", "first")
    second = git.text("commit-tree", tree, "-p", first, "-m", "empty second")
    third = git.text("commit-tree", tree, "-p", first, "-m", "empty third")
    assert git.text("rev-parse", first + "^{tree}") == git.text("rev-parse", second + "^{tree}")
    a = Authority(*pg_project, object_format=object_format, roots=dict.fromkeys([first, second, third], "commit"))
    for expected, proposed in ((None, first), (first, second), (first, third)):
        native = git.run("update-ref", "refs/heads/main", proposed, expected or "0" * len(first), check=False)
        actual = a.apply([update(old=oid(expected) if expected else ABSENT, new=oid(proposed))])
        assert (actual["status"] == "committed") == (native.returncode == 0)
    assert a.state()["oid"] == git.text("rev-parse", "refs/heads/main") == second
    native = git.run("update-ref", "--stdin", input=(
        f"start\nupdate refs/heads/main {third} {second}\n"
        f"update refs/heads/topic {second} {first}\nprepare\ncommit\n"
    ).encode(), check=False)
    actual = a.apply([
        update(old=oid(second), new=oid(third)),
        update(b"refs/heads/topic", oid(first), oid(second)),
    ])
    assert native.returncode != 0 and actual["status"] == "rejected"
    assert a.state()["oid"] == git.text("rev-parse", "refs/heads/main") == second
    assert a.state(b"refs/heads/topic") is None


def test_ref_authority_rejected_result_is_replayed_not_re_evaluated(authority):
    a, key = authority, str(uuid.uuid4())
    rejected = a.apply([update(old=oid(A), new=oid(B))], key=key)
    assert rejected["status"] == "rejected"
    a.apply([update(new=oid(A))])
    assert a.apply([update(old=oid(A), new=oid(B))], key=key) == rejected
    assert a.state()["oid"] == A
    assert a.count("version_ref_transactions") == 2


def test_ref_authority_incomplete_owner_initialization_cannot_publish(authority):
    a = authority
    a.pg.sql(f"DELETE FROM public.version_repository_refs WHERE project_id={literal(a.project)};")
    result = a.apply([update(new=oid(A))])
    assert result["status"] == "rejected" and result["reason"] == "missing_head"
    assert a.state() is None


def test_ref_authority_legacy_row_reparent_cannot_escape_fence(authority):
    a = authority
    other = "other-" + uuid.uuid4().hex
    a.pg.sql(f"""
      BEGIN;
      INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status)
      SELECT {literal(other)}, name, org_id, created_by, 'ready' FROM public.projects WHERE id={literal(a.project)};
      INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
      SELECT {literal('member-' + other)}, org_id, {literal(other)}, created_by, 'admin', created_by
      FROM public.projects WHERE id={literal(a.project)};
      UPDATE public.version_repositories SET authority='shadow' WHERE project_id={literal(a.project)};
      {a.pg.publish(a.project, '1' * 40, '1' * 40, A)}
      UPDATE public.version_repositories SET authority='native' WHERE project_id={literal(a.project)};
      COMMIT;
    """)
    for table in ("version_scope_state", "version_commits"):
        result = a.pg.sql(f"SET ROLE service_role; UPDATE public.{table} SET project_id={literal(other)} WHERE project_id={literal(a.project)};", check=False)
        assert result.returncode != 0 and "legacy_repository_publication_fenced" in result.stderr


def test_ref_authority_project_cascade_can_remove_fenced_history(authority):
    a = authority
    a.pg.sql(f"""
      UPDATE public.version_repositories SET authority='shadow' WHERE project_id={literal(a.project)};
      {a.pg.publish(a.project, '1' * 40, '1' * 40, A)}
      UPDATE public.version_repositories SET authority='native' WHERE project_id={literal(a.project)};
    """)
    a.apply([update(new=oid(A))])
    a.pg.sql(f"DELETE FROM public.projects WHERE id={literal(a.project)};")
    for table in TABLES:
        assert a.count(table) == 0


@pytest.mark.parametrize("updates", [
    [], {}, [None], [{"name_b64": b64(b"refs/heads/main")}],
    [update(new={"kind": "oid", "oid": A, "extra": "field"})],
    [update(new={"kind": "symbolic", "target_b64": "!"})],
    [update(new=symbolic(b"refs/heads/main"))],
    [update(b"HEAD", symbolic(b"refs/heads/main"), ABSENT)],
    [{"name_b64": "!", "expected": ABSENT, "new": oid(A)}],
    [{"name_b64": None, "expected": ABSENT, "new": oid(A)}],
])
def test_ref_authority_malformed_requests_never_get_success(authority, updates):
    result = authority.pg.sql(authority.query(updates), check=False)
    assert result.returncode != 0
    assert authority.count("version_ref_transactions") == 0


@pytest.mark.parametrize("actor,kind", [
    ("user:fixture", "user"), ("agent:fixture", "agent"),
    ("sync:fixture", "sync"), ("test:writer", "system"),
])
def test_ref_authority_audit_retains_admitted_actor(authority, actor, kind):
    a = authority
    result = a.apply([update(new=oid(A))], actor=actor)
    audit = json.loads(a.pg.value(f"""
      SELECT jsonb_build_object('actor',operator_id,'kind',operator_type,'detail',metadata)
      FROM public.audit_logs WHERE project_id={literal(a.project)};
    """))
    assert audit["actor"] == actor and audit["kind"] == kind
    assert audit["detail"]["ref_transaction_id"] == result["transaction_id"]
    assert audit["detail"]["refs"] == result["refs"]


def test_ref_authority_receipt_cannot_cross_project_boundary(authority):
    a = authority
    other = Authority(a.pg, a.pg.create_project())
    a.receipt = other.receipt
    result = a.pg.sql(a.query([update(new=oid(A))]), check=False)
    assert result.returncode != 0 and "invalid_receipt" in result.stderr
    assert a.count("version_ref_transactions") == 0
    assert a.state() is None


def test_ref_authority_no_backend_table_mutation_or_helper_execution(authority):
    a = authority
    for table in TABLES:
        assert a.pg.value(
            "SELECT has_table_privilege('service_role'," + literal("public." + table)
            + ",'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER');"
        ) == "f"
    assert a.pg.value("""
      SELECT bool_and(NOT has_function_privilege('service_role',oid,'EXECUTE'))
      FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname IN
      ('_version_ref_name_valid','_version_oid_valid','_version_ref_state_valid',
       '_version_receipt_roots_valid','_version_fence_legacy_publication');
    """) == "t"
