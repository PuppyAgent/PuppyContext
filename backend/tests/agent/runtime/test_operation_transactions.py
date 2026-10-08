"""Real SQL command atomicity, idempotency, fencing and authorization checks."""

import concurrent.futures
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def execution(postgres, submitted):
    args, run = submitted
    owned = postgres.rpc("claim", worker="operation-contract")
    assert owned["id"] == run["id"]
    identity = dict(run=run["id"], execution=owned["execution_id"], fence=owned["fence"])
    return args, identity


def start_tool(postgres, identity, **changes):
    return postgres.rpc(
        "begin_tool",
        **identity,
        **{
            "call": "read-1",
            "name": "read",
            "input": {"path": "a.md"},
            "checkpoint": {"state": "before"},
            "mutation": False,
            **changes,
        },
    )


def complete_tool(postgres, identity, **changes):
    return postgres.rpc(
        "complete_tool",
        **identity,
        **{
            "call": "read-1",
            "name": "read",
            "input": {"path": "a.md"},
            "result": {"pi_result": {"text": "saved"}, "checkpoint": {"state": "after"}},
            "checkpoint": {"state": "after"},
            **changes,
        },
    )


def test_tool_completion_replay_is_atomic_and_does_not_duplicate_events(postgres, execution):
    _, identity = execution
    start_tool(postgres, identity)
    first = complete_tool(postgres, identity)
    second = complete_tool(postgres, identity)
    assert first == second
    assert first["run"]["checkpoint"] == first["tool"]["result"]["checkpoint"]
    with pytest.raises(RuntimeError, match="agent_tool_result_conflict"):
        complete_tool(postgres, identity, result={"changed": True})
    assert postgres.rpc("execution_view", run=identity["run"])["run"] == first["run"]


def test_tool_completion_concurrency_has_one_durable_result(postgres, execution):
    _, identity = execution
    start_tool(postgres, identity)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: complete_tool(postgres, identity), range(4)))
    assert all(value == results[0] for value in results)


def test_failed_tool_transition_rolls_back_checkpoint_and_events(postgres, execution):
    _, identity = execution
    start_tool(postgres, identity, mutation=True)
    before = postgres.rpc("execution_view", run=identity["run"])
    with pytest.raises(RuntimeError, match="agent_tool_transition"):
        complete_tool(postgres, identity)
    after = postgres.rpc("execution_view", run=identity["run"])
    assert before == after


def test_approval_is_required_and_admitted_once(postgres, execution):
    args, identity = execution
    first = start_tool(postgres, identity, name="bash", input={"command": "true"}, mutation=True)
    assert first["tool"]["state"] == "waiting"
    assert first["run"]["state"] == "waiting_approval"
    waiting = start_tool(postgres, identity, name="bash", input={"command": "true"}, mutation=True)
    assert waiting["run"]["sequence"] == first["run"]["sequence"]
    postgres.rpc(
        "command",
        run=identity["run"],
        user=args["user"],
        command="approve",
        call="read-1",
        decision=str(uuid4()),
        allow=True,
    )
    allowed = start_tool(postgres, identity, name="bash", input={"command": "true"}, mutation=True)
    assert allowed["tool"]["state"] == "executing"
    assert allowed["run"]["state"] == "running"


@pytest.mark.parametrize(
    "change,code",
    [
        ("stop", "stop_requested"),
        ("deadline", "runtime_timeout"),
        ("membership", "authorization_revoked"),
        ("config", "authorization_revoked"),
    ],
)
@pytest.mark.parametrize("operation", ["begin_model", "begin_tool", "settle_model"])
def test_command_rejects_revocation_without_advancing_work(
    postgres, execution, change, code, operation
):
    args, identity = execution
    statements = {
        "stop": f"UPDATE agent_runs SET stop_requested=true WHERE id='{identity['run']}'",
        "deadline": f"UPDATE agent_runs SET deadline=clock_timestamp()-interval '1 second' WHERE id='{identity['run']}'",
        "membership": f"UPDATE org_members SET role='member' WHERE user_id='{args['user']}'",
        "config": f"UPDATE access_surfaces SET config='{{\"system_prompt\":\"revoked\"}}' WHERE id='{args['agent']}'",
    }
    postgres.sql(statements[change])
    before = postgres.rpc("execution_view", run=identity["run"])
    if operation == "begin_tool":
        result = start_tool(postgres, identity)
    else:
        extra = dict(request=str(uuid4()), limit=3) if operation == "begin_model" else {}
        result = postgres.rpc(operation, **identity, checkpoint={"next": True}, **extra)
    assert result["status"]["code"] == code
    assert result["run"]["sequence"] == before["run"]["sequence"]
    assert result["run"]["checkpoint"] == before["run"]["checkpoint"]
    assert not postgres.rpc("execution_view", run=identity["run"])["tools"]


def test_completion_preserves_executed_result_after_stop(postgres, execution):
    _, identity = execution
    start_tool(postgres, identity)
    postgres.sql(f"UPDATE agent_runs SET stop_requested=true WHERE id='{identity['run']}'")
    result = complete_tool(postgres, identity)
    assert result["tool"]["state"] == "completed"
    assert result["run"]["checkpoint"] == {"state": "after"}


def test_stale_owner_cannot_admit_or_complete_tool(postgres, execution):
    _, identity = execution
    start_tool(postgres, identity)
    postgres.sql(f"UPDATE agent_runs SET fence=fence+1 WHERE id='{identity['run']}'")
    for invoke in (start_tool, complete_tool):
        with pytest.raises(RuntimeError, match="agent_execution_fenced"):
            invoke(postgres, identity)


def test_model_request_identity_survives_ack_loss_and_rejects_changed_input(postgres, execution):
    _, identity = execution
    args = dict(**identity, request=str(uuid4()), checkpoint={"conversation": "first"}, limit=2)
    first = postgres.rpc("begin_model", **args)
    second = postgres.rpc("begin_model", **args)
    assert first["run"]["snapshot"]["model_calls"] == second["run"]["snapshot"]["model_calls"] == 1
    assert first["run"]["sequence"] == second["run"]["sequence"]
    with pytest.raises(RuntimeError, match="agent_batch_identity_conflict"):
        postgres.rpc("begin_model", **{**args, "checkpoint": {"different": True}})
    postgres.rpc("begin_model", **{**args, "request": str(uuid4())})
    with pytest.raises(RuntimeError, match="agent_model_limit"):
        postgres.rpc("begin_model", **{**args, "request": str(uuid4())})
    assert (
        postgres.rpc("execution_view", run=identity["run"])["run"]["snapshot"]["model_calls"] == 2
    )


def test_new_commands_are_service_role_only(postgres):
    signatures = [
        "agent_run_guard(uuid,uuid,bigint)",
        "agent_run_start_execution(uuid,uuid,bigint,jsonb,text,jsonb)",
        "agent_run_begin_model(uuid,uuid,bigint,uuid,jsonb,integer)",
        "agent_run_begin_tool(uuid,uuid,bigint,text,text,jsonb,jsonb,boolean)",
        "agent_run_complete_tool(uuid,uuid,bigint,text,text,jsonb,jsonb,jsonb)",
        "agent_run_settle_model(uuid,uuid,bigint,jsonb)",
        "agent_run_finish(uuid,uuid,bigint,text,text,jsonb,jsonb,boolean)",
        "get_version_publication_context(text,text,uuid)",
        "seal_version_capacity_batch(text,text,uuid,jsonb,text,jsonb)",
    ]
    for signature in signatures:
        for role, expected in (("anon", "f"), ("authenticated", "f"), ("service_role", "t")):
            assert (
                postgres.sql(
                    f"SELECT has_function_privilege('{role}','public.{signature}','EXECUTE')"
                )
                == expected
            )


def test_start_ack_replay_preserves_single_event(postgres, execution):
    _, identity = execution
    values = dict(**identity, checkpoint={"prepared": True}, billing=None, resource={"id": "owned"})
    first = postgres.rpc("start_execution", **values)
    second = postgres.rpc("start_execution", **values)
    assert first["run"]["sequence"] == second["run"]["sequence"]
    with pytest.raises(RuntimeError, match="agent_batch_identity_conflict"):
        postgres.rpc("start_execution", **{**values, "resource": {"id": "other"}})


def test_invalid_final_state_rolls_back_publication_event(postgres, execution):
    _, identity = execution
    before = postgres.rpc("execution_view", run=identity["run"])
    with pytest.raises(RuntimeError, match="agent_publication_unconfirmed"):
        postgres.rpc(
            "finish",
            **identity,
            state="succeeded",
            code="invalid",
            snapshot={},
            publication={"status": "pending"},
            cleaned=False,
        )
    after = postgres.rpc("execution_view", run=identity["run"])
    assert before == after
