"""Real SQL compatibility bridge: source head identity, not only tree equality.

Metadata/OIDs here are fixtures, not object durability or native authority.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.repository_hosting.harness.postgres import literal

pytestmark = pytest.mark.hosting_live
TREE = "1" * 40


def apply(pg, query):
    return json.loads(pg.value("SET ROLE service_role;" + query))


def head(pg, project, scope):
    return pg.value(
        "SELECT head_commit_id FROM public.version_scope_state "
        f"WHERE project_id={literal(project)} AND scope_path={literal(scope)}"
    )


@pytest.mark.parametrize("scope", ["", "docs"])
def test_same_tree_source_head_cas_rejects_stale_identity(pg_project, scope):
    pg, project = pg_project
    first, second = "a" * 40, "b" * 40
    query = pg.publish(project, TREE, TREE, first, expected_head="", scope=scope)
    assert apply(pg, query)["published"] is True
    stale = pg.publish(project, TREE, TREE, second, expected_head="", scope=scope)
    assert apply(pg, stale)["published"] is False
    assert head(pg, project, scope) == first
    good = pg.publish(project, TREE, TREE, second, expected_head=first, scope=scope)
    assert apply(pg, good)["published"] is True
    assert head(pg, project, scope) == second
    # Both metadata-only versions, and only those versions, commit their facts.
    for table in ("version_commits", "version_transactions", "version_outbox", "audit_logs"):
        assert pg.value(f"SELECT count(*) FROM public.{table} WHERE project_id={literal(project)}") == "2"


@pytest.mark.parametrize("scope", ["", "docs"])
@pytest.mark.parametrize("existing", [False, True], ids=["absent-row", "existing-row"])
def test_real_pg_same_tree_concurrent_source_head_cas_has_one_winner(pg_project, scope, existing):
    pg, project = pg_project
    old = ""
    if existing:
        old = "a" * 40
        assert apply(pg, pg.publish(project, TREE, TREE, old, expected_head="", scope=scope))["published"]
    gate = Barrier(8)

    def publish(index):
        wanted = f"{index + 100:040x}"
        gate.wait(timeout=10)
        return wanted, apply(pg, pg.publish(project, TREE, TREE, wanted, expected_head=old, scope=scope))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(8)))
    winners = [oid for oid, result in results if result["published"]]
    assert len(winners) == 1
    assert head(pg, project, scope) == winners[0]
    for table in ("version_commits", "version_transactions", "version_outbox", "audit_logs"):
        assert pg.value(f"SELECT count(*) FROM public.{table} WHERE project_id={literal(project)}") == str(1 + existing)


@pytest.mark.parametrize("scope", ["", "docs"])
def test_checked_publish_rollback_and_retry_keeps_original_head(pg_project, scope):
    pg, project = pg_project
    query = pg.publish(project, TREE, TREE, "a" * 40, expected_head="", scope=scope)
    pg.sql("BEGIN; SET LOCAL ROLE service_role;" + query + "ROLLBACK;")
    assert head(pg, project, scope) == ""
    assert apply(pg, query)["published"] is True
    assert apply(pg, query)["published"] is False


def test_legacy_unguarded_calls_remain_compatible_and_fence_checked_clients(pg_project):
    pg, project = pg_project
    # Old callers never supplied an expected head; retaining that contract must
    # not be confused with granting those calls OID-CAS semantics retroactively.
    for oid in ("a" * 40, "b" * 40):
        query = pg.publish(project, TREE, TREE, oid).replace(
            "publish_version_project_update", "publish_mut_project_update",
        )
        assert apply(pg, query)["published"] is True
    assert not apply(pg, pg.publish(project, TREE, TREE, "c" * 40, expected_head="a" * 40))["published"]
    assert head(pg, project, "") == "b" * 40


def test_wrong_tree_still_rejects_when_head_matches(pg_project):
    pg, project = pg_project
    assert not apply(pg, pg.publish(project, "2" * 40, TREE, "a" * 40, expected_head=""))["published"]
    assert head(pg, project, "") == ""
