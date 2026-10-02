import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.version_engine.infrastructure.supabase.db_names import (
    COMMIT_HISTORY_TABLE,
    VERSION_OUTBOX_TABLE,
)
from tests.repository_hosting.harness.postgres import Postgres, literal

pytestmark = pytest.mark.hosting_live


@pytest.fixture
def pg_project():
    pg = Postgres()
    key, user = uuid.uuid4().hex, str(uuid.uuid4())
    project, org = "hosting-" + key, "hosting-org-" + key
    pg.sql(f"""
      BEGIN;
      INSERT INTO auth.users(id,aud,role,email,encrypted_password,email_confirmed_at,raw_app_meta_data,raw_user_meta_data,created_at,updated_at)
      VALUES ({literal(user)},'authenticated','authenticated',{literal(key + "@example.test")},'',now(),'{{}}','{{}}',now(),now());
      INSERT INTO public.organizations(id,name,slug,type,plan,seat_limit,created_by)
      VALUES ({literal(org)},'Hosting tests',{literal(org)},'team','enterprise',5,{literal(user)});
      INSERT INTO public.org_members(id,org_id,user_id,role) VALUES ({literal("member-" + key)},{literal(org)},{literal(user)},'owner');
      INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status,version_root_hash)
      VALUES ({literal(project)},'Hosting tests',{literal(org)},{literal(user)},'ready',{literal("1" * 40)});
      INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
      VALUES ({literal("pm-" + key)},{literal(org)},{literal(project)},{literal(user)},'admin',{literal(user)});
      COMMIT;
    """)
    return pg, project


def test_real_pg_concurrent_root_cas_has_exactly_one_winner(pg_project):
    pg, project = pg_project
    barrier = Barrier(8)

    def publish(i):
        barrier.wait(timeout=10)
        return json.loads(
            pg.value(pg.publish(project, "1" * 40, f"{i + 2:040x}", f"{i + 100:040x}"))
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(publish, range(8)))
    assert sum(row["published"] for row in outcomes) == 1
    assert (
        pg.value(
            f"SELECT count(*) FROM public.{COMMIT_HISTORY_TABLE} WHERE project_id={literal(project)}"
        )
        == "1"
    )
    assert (
        pg.value(
            f"SELECT count(*) FROM public.{VERSION_OUTBOX_TABLE} WHERE project_id={literal(project)}"
        )
        == "1"
    )


def test_real_pg_rollback_reverts_root_history_and_outbox(pg_project):
    pg, project = pg_project
    pg.sql("BEGIN;" + pg.publish(project, "1" * 40, "2" * 40, "a" * 40) + "ROLLBACK;")
    assert (
        pg.value(f"SELECT version_root_hash FROM public.projects WHERE id={literal(project)}")
        == "1" * 40
    )
    for table in (COMMIT_HISTORY_TABLE, VERSION_OUTBOX_TABLE, "version_transactions"):
        assert (
            pg.value(f"SELECT count(*) FROM public.{table} WHERE project_id={literal(project)}")
            == "0"
        )


def test_real_pg_same_operation_stale_root_does_not_duplicate_event(pg_project):
    pg, project = pg_project
    query = pg.publish(project, "1" * 40, "2" * 40, "a" * 40)
    assert json.loads(pg.value(query))["published"] is True
    assert json.loads(pg.value(query))["published"] is False
    assert (
        pg.value(
            f"SELECT count(*) FROM public.{VERSION_OUTBOX_TABLE} WHERE project_id={literal(project)}"
        )
        == "1"
    )


def test_legacy_and_current_clients_share_existing_root_history_and_events(pg_project):
    pg, project = pg_project
    # A legacy writer uses the actual compatibility RPC retained in B1.
    legacy = pg.publish(project, "1" * 40, "2" * 40, "a" * 40).replace(
        "public.publish_version_project_update(", "public.publish_mut_project_update("
    )
    assert json.loads(pg.value(legacy))["published"] is True
    assert (
        pg.value(
            f"SELECT version_root_hash = mut_root_hash FROM public.projects WHERE id={literal(project)}"
        )
        == "t"
    )
    assert (
        pg.value(
            f"SELECT commit_id FROM public.{COMMIT_HISTORY_TABLE} WHERE project_id={literal(project)}"
        )
        == "a" * 40
    )
    # The next new writer must append while preserving the old commit identity.
    assert (
        json.loads(pg.value(pg.publish(project, "2" * 40, "3" * 40, "b" * 40)))["published"] is True
    )
    for table in ("mut_commits", COMMIT_HISTORY_TABLE):
        assert (
            pg.value(
                f"SELECT string_agg(commit_id, ',' ORDER BY commit_id) FROM public.{table} WHERE project_id={literal(project)}"
            )
            == "a" * 40 + "," + "b" * 40
        )
    assert (
        pg.value(f"SELECT mut_root_hash FROM public.projects WHERE id={literal(project)}")
        == "3" * 40
    )
    assert (
        pg.value(
            f"SELECT count(*) FROM public.{VERSION_OUTBOX_TABLE} WHERE project_id={literal(project)}"
        )
        == "2"
    )


def test_new_and_legacy_column_writers_preserve_each_others_saved_root(pg_project):
    pg, project = pg_project
    for column, expected in (("mut_root_hash", "4" * 40), ("version_root_hash", "5" * 40)):
        pg.sql(
            f"UPDATE public.projects SET {column}={literal(expected)} WHERE id={literal(project)}"
        )
        assert (
            pg.value(
                f"SELECT version_root_hash || ':' || mut_root_hash FROM public.projects WHERE id={literal(project)}"
            )
            == expected + ":" + expected
        )


@pytest.mark.hosting_gap(
    "current root-hash CAS cannot reject an old head when both commits share a tree"
)
def test_same_tree_different_commits_require_head_cas(pg_project):
    pg, project = pg_project
    first = json.loads(pg.value(pg.publish(project, "1" * 40, "1" * 40, "a" * 40)))
    second = json.loads(pg.value(pg.publish(project, "1" * 40, "1" * 40, "b" * 40)))
    assert first["published"] is True
    assert second["published"] is False
