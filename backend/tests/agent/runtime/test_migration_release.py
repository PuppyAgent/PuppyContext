"""Expand / pre-activation rollback / reapply on an owned scratch database."""

import importlib.util
from uuid import uuid4

import pytest

from tests.agent.runtime.conftest import ROOT, Database

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "fault", [None, "missing_command", "disabled_guard", "deletion_guard", "proof_bypass"]
)
def test_hosted_release_gate_checks_workspace_and_proof_contracts(postgres, fault):
    """The same read-only SQL used by CD rejects partial/insecure installs."""
    mutations = {
        "missing_command": "ALTER FUNCTION agent_run_workspace_due(integer) RENAME TO missing_workspace_due",
        "disabled_guard": "ALTER TABLE version_ref_transactions DISABLE TRIGGER version_publish_object_proofs",
        "deletion_guard": "ALTER TABLE projects DISABLE TRIGGER agent_workspace_project_deleting",
        "proof_bypass": "GRANT INSERT ON version_object_proofs TO service_role",
    }
    expected = {
        "missing_command": "CLOUD_AGENT_FUNCTION_MISSING",
        "disabled_guard": "CLOUD_AGENT_PUBLICATION_GUARD_MISSING",
        "deletion_guard": "CLOUD_AGENT_PUBLICATION_GUARD_MISSING",
        "proof_bypass": "CLOUD_AGENT_PROOF_DIRECT_ACCESS",
    }
    sql = (ROOT / "supabase/tests/_support/cloud_agent_contracts.inc").read_text()
    if fault is None:
        postgres.sql(sql)
    else:
        # The failed psql session rolls this fixture mutation back on exit.
        sql = sql.replace("BEGIN READ ONLY;", "BEGIN; " + mutations[fault] + ";")
        with pytest.raises(RuntimeError, match=expected[fault]):
            postgres.sql(sql)
        postgres.sql((ROOT / "supabase/tests/_support/cloud_agent_contracts.inc").read_text())


def test_additive_migration_preserves_existing_agents_and_pre_activation_rollback(
    postgres, submitted
):
    spec = importlib.util.spec_from_file_location(
        "agent_migration_sql", ROOT / "scripts/testing/native_postgres.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = "agent_migration_" + uuid4().hex
    postgres.sql(f"CREATE DATABASE {database}")
    scratch = Database(postgres.url.rsplit("/", 1)[0] + "/" + database)
    try:
        scratch.sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
        migrations = sorted((ROOT / "supabase/migrations").glob("*.sql"))
        agent = [
            p
            for p in migrations
            if p.name
            in {
                "20261006060000_expand_cloud_agent_runs.sql",
                "20261006060100_fence_cloud_agent_publication.sql",
            }
        ]
        for migration in migrations:
            if (
                migration not in agent
                and migration.name < "20261009000000"
                and migration.name != "20261008010000_bound_agent_data_operations.sql"
            ):
                scratch.sql(module.product_migration_sql(migration))
        previous_fence = scratch.sql(
            "SELECT pg_get_functiondef('public._version_assert_write_lease(text,uuid,text)'::regprocedure)"
        )
        args, _ = submitted
        user, project, agent_id = args["user"], args["project"], args["agent"]
        org = postgres.row(f"SELECT org_id FROM projects WHERE id='{project}'")["org_id"]
        scratch.sql(f"""
            BEGIN;
            INSERT INTO auth.users(id) VALUES('{user}');
            INSERT INTO organizations(id,name,slug,created_by) VALUES('{org}','Preserved','preserved-{org}','{user}');
            INSERT INTO org_members(org_id,user_id,role) VALUES('{org}','{user}','owner');
            INSERT INTO projects(id,name,org_id,created_by,lifecycle_status) VALUES('{project}','Preserved','{org}','{user}','ready');
            INSERT INTO project_members(project_id,org_id,user_id,role) VALUES('{project}','{org}','{user}','admin');
            INSERT INTO access_surfaces(id,project_id,org_id,kind,name,created_by,config)
              VALUES('{agent_id}','{project}','{org}','agent','Existing','{user}','{{"system_prompt":"keep exact config","llm_model":"saved-model"}}');
            INSERT INTO chat_sessions(id,user_id,agent_id,mode) VALUES('existing-chat','{user}','{agent_id}','agent');
            INSERT INTO chat_messages(session_id,role,content) VALUES('existing-chat','assistant','Keep existing display history');
            COMMIT;
        """)
        before = scratch.row(f"SELECT config FROM access_surfaces WHERE id='{agent_id}'")
        for migration in agent:
            scratch.sql(migration.read_text())
        assert scratch.row(f"SELECT config FROM access_surfaces WHERE id='{agent_id}'") == before
        # Only before accepting any new runs. Production rollback retains tables
        # after traffic; deleting accepted receipts would violate durability.
        assert scratch.sql("SELECT count(*) FROM agent_runs") == "0"
        scratch.sql(
            """BEGIN;
            DROP TABLE agent_runs CASCADE;
            DROP TABLE IF EXISTS agent_run_executions,agent_run_tools,agent_run_events CASCADE;
            DO $$ DECLARE fn record; BEGIN
                FOR fn IN SELECT oid::regprocedure AS signature FROM pg_proc
                  WHERE pronamespace='public'::regnamespace AND proname LIKE 'agent_run_%'
                LOOP EXECUTE format('DROP FUNCTION %s CASCADE',fn.signature); END LOOP;
            END $$;
        """
            + previous_fence
            + "; COMMIT;"
        )
        assert scratch.sql("SELECT to_regclass('public.agent_runs') IS NULL") == "t"
        assert (
            scratch.sql(
                "SELECT pg_get_functiondef('public._version_assert_write_lease(text,uuid,text)'::regprocedure)"
            )
            == previous_fence
        )
        assert scratch.row(f"SELECT config FROM access_surfaces WHERE id='{agent_id}'") == before
        assert (
            scratch.sql("SELECT content FROM chat_messages WHERE session_id='existing-chat'")
            == "Keep existing display history"
        )
        for migration in agent:
            scratch.sql(migration.read_text())
        assert scratch.sql("SELECT to_regclass('public.agent_runs') IS NOT NULL") == "t"
        # Upgrade the new contracts over populated saved configuration and
        # completed old-runtime receipts. Old workers must be drained before
        # activation; historical policy JSON is preserved, never synthesized.
        old = scratch.rpc("submit", **{**args, "policy": {"model": "saved-model"}})
        scratch.sql(f"UPDATE agent_runs SET state='failed' WHERE id='{old['id']}'")
        receipt = scratch.row(f"SELECT * FROM agent_runs WHERE id='{old['id']}'")
        messages = scratch.sql("SELECT jsonb_agg(m ORDER BY id) FROM chat_messages m")
        fence = scratch.sql(
            "SELECT pg_get_functiondef('public.agent_run_publication_fence(uuid,uuid,bigint,text)'::regprocedure)"
        )
        bounded = ROOT / "supabase/migrations/20261008010000_bound_agent_data_operations.sql"
        # Rehearse a transaction rollback before committing this immutable SQL.
        scratch.sql(bounded.read_text().replace("COMMIT;", "ROLLBACK;"))
        assert scratch.sql("SELECT to_regclass('public.authorization_revisions') IS NULL") == "t"
        assert (
            scratch.sql(
                "SELECT pg_get_functiondef('public.agent_run_publication_fence(uuid,uuid,bigint,text)'::regprocedure)"
            )
            == fence
        )
        scratch.sql(bounded.read_text())
        assert scratch.row(f"SELECT * FROM agent_runs WHERE id='{old['id']}'") == receipt
        assert scratch.row(f"SELECT config FROM access_surfaces WHERE id='{agent_id}'") == before
        assert scratch.sql("SELECT jsonb_agg(m ORDER BY id) FROM chat_messages m") == messages
        context = scratch.rpc("context", user=user, project=project, agent=agent_id)
        assert context["surface"]["config"] == before["config"]
        assert context["facts"]["project_role"] == "admin"
        assert context["revision"]["project"] == project
        # Additive operation transactions preserve all populated records, and
        # a rolled-back installation exposes no half-installed command API.
        for migration in migrations:
            if migration.name < "20261009000000":
                continue
            scratch.sql(migration.read_text().replace("COMMIT;", "ROLLBACK;"))
            scratch.sql(migration.read_text())
            assert scratch.row(f"SELECT * FROM agent_runs WHERE id='{old['id']}'") == receipt
            assert (
                scratch.row(f"SELECT config FROM access_surfaces WHERE id='{agent_id}'") == before
            )
            assert scratch.sql("SELECT jsonb_agg(m ORDER BY id) FROM chat_messages m") == messages
    finally:
        postgres.sql(f"DROP DATABASE {database} WITH (FORCE)")
