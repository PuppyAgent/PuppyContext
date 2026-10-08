"""Real SQL ownership, expiry and late-provider-response contracts."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration
BINDING = {
    "generation": 1,
    "object_format": "sha1",
    "target_ref": "refs/heads/main",
    "artifact": "immutable",
}


@pytest.fixture
def owned(postgres, submitted):
    args, first = submitted
    run = postgres.rpc("claim", worker="workspace-test")
    assert run["id"] == first["id"]
    identity = dict(run=run["id"], execution=run["execution_id"], fence=run["fence"])
    yield args, run, identity
    # Synthetic resources have no provider I/O; keep due queries isolated.
    postgres.sql(
        f"UPDATE agent_session_workspaces SET state='retired',retire_after=NULL,operation_until=NULL WHERE session_id='{run['session_id']}'"
    )


def transition(db, identity, row, state, resource=None):
    return db.rpc(
        "workspace_transition",
        **identity,
        generation=row["generation"],
        version=row["version"],
        state=state,
        resource=resource,
    )


def park(db, owned):
    _, run, identity = owned
    row = db.rpc("workspace_acquire", **identity, binding=BINDING)
    resource = {
        "resource_id": "owned-fixture",
        "session_id": identity["execution"],
        "provider": "docker",
    }
    row = transition(db, identity, row, "allocating", resource)
    row = transition(db, identity, row, "running", resource)
    db.rpc("write", **identity, kind="publication", patch={"publication": {"status": "no_changes"}})
    row = transition(db, identity, row, "pausing")
    row = transition(db, identity, row, "paused")
    assert row["retire_after"] is None  # Active run still owns finalization.
    db.rpc(
        "finish",
        **identity,
        state="succeeded",
        code="completed",
        snapshot={},
        publication={"status": "no_changes"},
        cleaned=True,
    )
    return db.row(f"SELECT * FROM agent_session_workspaces WHERE session_id='{run['session_id']}'")


def test_idle_deadline_is_six_hours_after_release_and_resources_survive(postgres, owned):
    row = park(postgres, owned)
    interval = postgres.row(
        f"SELECT extract(epoch FROM retire_after-idle_since)::integer AS seconds FROM agent_session_workspaces WHERE session_id='{row['session_id']}'"
    )
    assert interval["seconds"] == 21600
    assert row["state"] == "paused" and row["resource"]["resource_id"] == "owned-fixture"
    assert postgres.rpc("workspace_due", limit=20) == []


def test_new_turn_cancels_idle_deadline_and_reuses_identity(postgres, owned):
    row = park(postgres, owned)
    args, _, _ = owned
    next_run = postgres.rpc(
        "submit", **{**args, "session": row["session_id"], "request": str(uuid4())}
    )
    claimed = postgres.rpc("claim", worker="next-turn")
    assert claimed["id"] == next_run["id"]
    acquired = postgres.rpc(
        "workspace_acquire",
        run=claimed["id"],
        execution=claimed["execution_id"],
        fence=claimed["fence"],
        binding=BINDING,
    )
    assert acquired["generation"] == row["generation"]
    assert acquired["resource"] == row["resource"]
    assert acquired["state"] == "resuming" and acquired["retire_after"] is None


def test_due_claim_is_exclusive_and_late_ack_cannot_retire_new_generation(postgres, owned):
    row = park(postgres, owned)
    sid = row["session_id"]
    postgres.sql(
        f"UPDATE agent_session_workspaces SET retire_after=clock_timestamp()-interval '1 second' WHERE session_id='{sid}'"
    )
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: postgres.rpc("workspace_due", limit=1), range(2)))
    claimed = [item for batch in claims for item in batch]
    assert len(claimed) == 1
    operation = claimed[0]
    assert not postgres.rpc(
        "workspace_retired", session=sid, generation=row["generation"], operation=str(uuid4())
    )
    assert postgres.rpc(
        "workspace_retired",
        session=sid,
        generation=row["generation"],
        operation=operation["operation_id"],
    )
    assert not postgres.rpc(
        "workspace_retired",
        session=sid,
        generation=row["generation"],
        operation=operation["operation_id"],
    )


def test_queued_turn_protects_workspace_from_retirement(postgres, owned):
    row = park(postgres, owned)
    args, _, _ = owned
    postgres.sql(
        f"UPDATE agent_session_workspaces SET retire_after=clock_timestamp()-interval '1 second' WHERE session_id='{row['session_id']}'"
    )
    postgres.rpc("submit", **{**args, "session": row["session_id"], "request": str(uuid4())})
    assert postgres.rpc("workspace_due", limit=20) == []


def test_pause_requires_confirmed_publication_and_delete_requires_retirement(postgres, owned):
    _, _, identity = owned
    row = postgres.rpc("workspace_acquire", **identity, binding=BINDING)
    row = transition(postgres, identity, row, "running", {"resource_id": "only-copy"})
    with pytest.raises(RuntimeError, match="publication_unconfirmed"):
        transition(postgres, identity, row, "pausing")
    with pytest.raises(RuntimeError, match="cleanup_required"):
        postgres.sql(f"DELETE FROM agent_session_workspaces WHERE session_id='{row['session_id']}'")
    row = transition(postgres, identity, row, "retained")
    assert postgres.rpc("workspace_due", limit=20) == []


def test_transition_replay_is_idempotent_and_stale_version_rejected(postgres, owned):
    _, _, identity = owned
    row = postgres.rpc("workspace_acquire", **identity, binding=BINDING)
    active = transition(postgres, identity, row, "running", {"resource_id": "same"})
    assert transition(postgres, identity, row, "running", {"resource_id": "same"}) == active
    with pytest.raises(RuntimeError, match="transition_invalid"):
        transition(postgres, identity, row, "running", {"resource_id": "different"})


def test_workspace_tables_and_commands_are_service_only(postgres):
    assert (
        postgres.sql(
            "SELECT has_table_privilege('authenticated','agent_session_workspaces','SELECT')"
        )
        == "f"
    )
    assert (
        postgres.sql(
            "SELECT has_function_privilege('anon','agent_run_workspace_due(integer)','EXECUTE')"
        )
        == "f"
    )


@pytest.mark.parametrize("seconds", [-1, 0, 1])
def test_retirement_exact_idle_boundary(postgres, owned, seconds):
    row = park(postgres, owned)
    postgres.sql(
        f"UPDATE agent_session_workspaces SET retire_after=clock_timestamp()+interval '{-seconds} seconds' WHERE session_id='{row['session_id']}'"
    )
    assert bool(postgres.rpc("workspace_due", limit=20)) == (seconds >= 0)


def test_lost_retirement_ack_is_retried_even_when_next_turn_is_queued(postgres, owned):
    row = park(postgres, owned)
    postgres.sql(
        f"UPDATE agent_session_workspaces SET retire_after=clock_timestamp()-interval '1 second' WHERE session_id='{row['session_id']}'"
    )
    first = postgres.rpc("workspace_due", limit=20)[0]
    args, _, _ = owned
    postgres.rpc("submit", **{**args, "session": row["session_id"], "request": str(uuid4())})
    postgres.sql(
        f"UPDATE agent_session_workspaces SET operation_until=clock_timestamp()-interval '1 second' WHERE session_id='{row['session_id']}'"
    )
    retry = postgres.rpc("workspace_due", limit=20)[0]
    assert retry["operation_id"] != first["operation_id"]
    assert retry["resource"] == first["resource"]
    assert not postgres.rpc(
        "workspace_retired",
        session=row["session_id"],
        generation=row["generation"],
        operation=first["operation_id"],
    )
    assert postgres.rpc(
        "workspace_retired",
        session=row["session_id"],
        generation=row["generation"],
        operation=retry["operation_id"],
    )


@pytest.mark.parametrize("owner", ["session", "project"])
def test_owner_deletion_retains_provider_cleanup_ownership(postgres, owned, owner):
    row = park(postgres, owned)
    if owner == "session":
        postgres.sql(f"DELETE FROM chat_sessions WHERE id='{row['session_id']}'")
    else:
        postgres.sql(f"DELETE FROM projects WHERE id='{row['project_id']}'")
    pending = postgres.rpc("workspace_due", limit=20)
    assert len(pending) == 1
    assert pending[0]["resource"] == row["resource"]
    assert pending[0]["state"] == "cleanup_pending"
    assert postgres.rpc(
        "workspace_retired",
        session=row["session_id"],
        generation=row["generation"],
        operation=pending[0]["operation_id"],
    )
    postgres.sql(f"DELETE FROM agent_session_workspaces WHERE session_id='{row['session_id']}'")


def test_revoked_run_cannot_acquire_a_paused_workspace(postgres, owned):
    row = park(postgres, owned)
    args, _, _ = owned
    postgres.rpc("submit", **{**args, "session": row["session_id"], "request": str(uuid4())})
    run = postgres.rpc("claim", worker="revoked-turn")
    postgres.sql(f"UPDATE access_surfaces SET status='paused' WHERE id='{args['agent']}'")
    with pytest.raises(RuntimeError, match="admission_denied"):
        postgres.rpc(
            "workspace_acquire",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            binding=BINDING,
        )
    assert (
        postgres.row(
            f"SELECT state FROM agent_session_workspaces WHERE session_id='{row['session_id']}'"
        )["state"]
        == "paused"
    )
