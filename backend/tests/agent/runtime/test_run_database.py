import concurrent.futures
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def ownership(run):
    return dict(run=run["id"], execution=run["execution_id"], fence=run["fence"])


def test_concurrent_submit_and_lease_fencing(postgres, submitted):
    arguments, run = submitted
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: postgres.rpc("submit", **arguments), range(8)))
    assert {row["id"] for row in results} == {run["id"]}
    assert (
        postgres.sql(f"SELECT count(*) FROM chat_messages WHERE session_id='{run['session_id']}'")
        == "1"
    )
    with pytest.raises(RuntimeError, match="agent_request_conflict"):
        postgres.rpc("submit", **{**arguments, "digest": "b" * 64})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: postgres.rpc("claim", worker=str(uuid4())), range(2)))
    winner = next(row for row in claims if row)
    assert sum(row is not None for row in claims) == 1
    postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{run['id']}'"
    )
    successor = postgres.rpc("claim", worker="takeover")
    assert successor["fence"] == winner["fence"] + 1
    with pytest.raises(RuntimeError, match="agent_execution_fenced"):
        postgres.rpc("write", **ownership(winner), kind="text", payload={"delta": "stale"})
    postgres.rpc("write", **ownership(successor), kind="text", payload={"delta": "valid"})


def test_tool_approval_receipt_and_stop(postgres, submitted):
    arguments, _ = submitted
    run = postgres.rpc("claim", worker="approval")
    key = ownership(run)
    tool = dict(call="call_1", name="write", input={"path": "hello.md"}, state="waiting")
    postgres.rpc("tool", **key, **tool)
    postgres.rpc("write", **key, kind="state", patch={"state": "waiting_approval"})
    decision = dict(
        run=run["id"],
        user=arguments["user"],
        command="approve",
        call="call_1",
        decision=str(uuid4()),
        allow=True,
    )
    postgres.rpc("command", **decision)
    postgres.rpc("command", **decision)
    with pytest.raises(RuntimeError, match="agent_approval_conflict"):
        postgres.rpc("command", **{**decision, "allow": False})
    postgres.rpc("tool", **key, **{**tool, "state": "executing"})
    postgres.rpc("tool", **key, **{**tool, "state": "completed"}, result={"content": "ok"})
    receipt = postgres.rpc("tool", **key, **tool)
    assert receipt["state"] == "completed"
    postgres.rpc("command", run=run["id"], user=arguments["user"], command="stop")
    with pytest.raises(RuntimeError, match="agent_stop_requested"):
        postgres.rpc("tool", **key, call="call_2", name="bash", input={}, state="waiting")
    with pytest.raises(RuntimeError, match="agent_run_not_found"):
        postgres.rpc("command", run=run["id"], user=str(uuid4()), command="stop")


def test_busy_retention_and_no_false_success(postgres, submitted):
    arguments, run = submitted
    with pytest.raises(RuntimeError, match="agent_session_busy"):
        postgres.rpc(
            "submit", **{**arguments, "session": run["session_id"], "request": str(uuid4())}
        )
    claim = postgres.rpc("claim", worker="retention")
    with pytest.raises(RuntimeError, match="agent_publication_unconfirmed"):
        postgres.rpc("write", **ownership(claim), kind="terminal", patch={"state": "succeeded"})
    postgres.sql(
        f"SELECT agent_run_event('{run['id']}', 'text', '{{}}') FROM generate_series(1,520)"
    )
    assert (
        postgres.sql(f"SELECT count(*) FROM agent_run_events WHERE run_id='{run['id']}'") == "512"
    )
    postgres.rpc(
        "write",
        **ownership(claim),
        kind="terminal",
        patch={"state": "succeeded", "publication": {"status": "no_changes"}},
    )
    assert (
        postgres.row(f"SELECT state FROM agent_runs WHERE id='{run['id']}'")["state"] == "succeeded"
    )


def test_internal_tables_and_functions_are_not_user_apis(postgres, submitted):
    for statement in (
        "SELECT * FROM public.agent_runs",
        "SELECT public.agent_run_claim('intruder')",
    ):
        with pytest.raises(RuntimeError, match="permission denied"):
            postgres.sql("SET ROLE authenticated; " + statement)


def test_real_postgres_restart_preserves_receipt(postgres, submitted, control_api):
    import subprocess
    from pathlib import Path

    args, run = submitted
    directory = postgres.sql("SHOW data_directory")
    assert Path(directory).parent.name.startswith("hosting-native-")
    subprocess.run(
        [
            "/opt/homebrew/opt/postgresql@17/bin/pg_ctl",
            "-D",
            directory,
            "-l",
            str(Path(directory).parent / "server.log"),
            "-m",
            "fast",
            "-w",
            "restart",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # PostgREST must reconnect its actual pool after the database restart.
    import time

    from postgrest.exceptions import APIError

    for attempt in range(150):
        try:
            response = control_api.table("agent_runs").select("id").eq("id", run["id"]).execute()
            assert response.data[0]["id"] == run["id"]
            break
        except APIError:
            if attempt == 149:
                raise
            time.sleep(0.1)
    assert postgres.rpc("submit", **args)["id"] == run["id"]
    assert (
        postgres.sql(f"SELECT count(*) FROM chat_messages WHERE session_id='{run['session_id']}'")
        == "1"
    )


def test_transaction_rollback_and_parent_delete_guard(postgres, submitted):
    _, run = submitted
    for table, identity in [
        ("chat_sessions", run["session_id"]),
        ("access_surfaces", run["agent_id"]),
        ("projects", run["project_id"]),
    ]:
        with pytest.raises(RuntimeError, match="agent_run_cleanup_required"):
            postgres.sql(f"DELETE FROM {table} WHERE id='{identity}'")
    # Rehearse additive schema removal in a rolled-back transaction. Existing
    # receipts and original Agent configuration must survive the rollback.
    postgres.sql("BEGIN; DROP TABLE agent_run_events; ROLLBACK;")
    assert postgres.sql(f"SELECT count(*) FROM agent_run_events WHERE run_id='{run['id']}'") == "1"
    postgres.sql(
        f"UPDATE agent_runs SET state='failed' WHERE id='{run['id']}'; DELETE FROM chat_sessions WHERE id='{run['session_id']}'"
    )
    assert postgres.sql(f"SELECT count(*) FROM agent_runs WHERE id='{run['id']}'") == "0"


def test_publication_fence_rechecks_execution_and_configuration(postgres, submitted):
    _, run = submitted
    original = postgres.rpc("claim", worker="old-owner")
    postgres.rpc("write", **ownership(original), kind="state", patch={"state": "publishing"})
    postgres.rpc("publication_fence", **ownership(original), project=run["project_id"])
    postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{run['id']}'"
    )
    current = postgres.rpc("claim", worker="new-owner")
    with pytest.raises(RuntimeError, match="agent_execution_fenced"):
        postgres.rpc("publication_fence", **ownership(original), project=run["project_id"])
    postgres.sql(f"UPDATE access_surfaces SET status='paused' WHERE id='{run['agent_id']}'")
    with pytest.raises(RuntimeError, match="agent_configuration_changed"):
        postgres.rpc("publication_fence", **ownership(current), project=run["project_id"])
