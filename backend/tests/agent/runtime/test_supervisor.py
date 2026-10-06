"""Pi + PostgREST + PostgreSQL + MinIO + canonical publisher in one run."""

import asyncio
import json
from uuid import uuid4

import pytest

from src.platform.access.adapters.agent.runtime.admission import Admission
from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints
from src.platform.access.adapters.agent.runtime.models import SubmitRun
from src.platform.access.adapters.agent.runtime.publication import Publication
from src.platform.access.adapters.agent.runtime.repository import RunRepository
from src.platform.access.adapters.agent.runtime.runner import RunSupervisor
from src.platform.access.adapters.agent.service import AgentService
from src.platform.billing.runtime import get_runtime_metering_service
from src.platform.managed_ai.contracts import InferenceDone, InferenceRun, ModelChunk
from src.platform.project.write_lease import ProjectWriteLease
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore

pytestmark = pytest.mark.integration


class ModelFixture:
    def __init__(self):
        self.calls = 0

    async def completion(self, user, request, body):
        self.calls += 1
        call = self.calls == 1
        delta = (
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "write_fixture",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps(
                                {"path": "result.txt", "content": "durable result"}
                            ),
                        },
                    }
                ],
            }
            if call
            else {"role": "assistant", "content": "Saved."}
        )

        async def events():
            frame = {
                "id": "fixture_" + str(self.calls),
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield ModelChunk(frame)
            yield ModelChunk(
                {
                    **frame,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )
            yield InferenceDone()

        async def close():
            pass

        return InferenceRun(str(uuid4()), events(), close)


def worker_factory(execution, project):
    return PiWorker(execution, project, provider="docker", store=InMemoryExecutionSessionStore())


@pytest.fixture
async def prepared(services, postgres, submitted, request):
    from types import SimpleNamespace

    client, storage, container = services
    args, first = submitted
    postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE id='{first['id']}'")
    project, user, agent = args["project"], args["user"], args["agent"]
    ops = container.product_operations()
    if getattr(request, "param", "legacy") == "native":
        from tests.repository_hosting.integration.test_repository_file_policy import (
            enroll_file_policy,
        )
        from tests.repository_hosting.integration.test_repository_logical_billing import (
            enroll_billing,
        )

        org = postgres.row(f"SELECT org_id FROM projects WHERE id='{project}'")["org_id"]
        postgres.sql(f"""
            INSERT INTO version_repositories(project_id,authority,object_format) VALUES('{project}','native','sha1');
            INSERT INTO version_repository_refs(project_id,name,object_format,symbolic_target)
                VALUES('{project}',decode('48454144','hex'),'sha1',decode('726566732f68656164732f6d61696e','hex'));
            INSERT INTO version_organization_capacity(org_id,initialized) VALUES('{org}',true);
            INSERT INTO version_repository_capacity(project_id,org_id,initialized) VALUES('{project}','{org}',true);
        """)
        enrolled = enroll_billing(SimpleNamespace(pg=postgres, project=project, org=org))
        enroll_file_policy(enrolled, maximum=65536)
        postgres.sql(
            f"UPDATE organization_entitlements SET entitlements=jsonb_set(entitlements,'{{limits,storage.max_bytes}}','1048576'::jsonb) WHERE org_id='{org}'"
        )
    else:
        async with ProjectWriteLease(project, "agent.fixture"):
            await container.write_engine().initialize_project_tree(project)
            await ops.write_file(
                project, "hello.txt", b"hello", who="user:" + user, source_channel="access_git"
            )
    postgres.sql(
        f"INSERT INTO access_surfaces(project_id,org_id,kind,name,created_by) SELECT id,org_id,'git_remote','Git','{user}' FROM projects WHERE id='{project}'"
    )
    config = {
        "name": "Agent fixture",
        "llm_model": "fixture",
        "bash_view": {"path_prefix": "", "excludes": [], "max_mode": "rw"},
    }
    client.table("access_surfaces").update({"config": config}).eq("id", agent).execute()
    repo = RunRepository(client)
    admission = Admission(repo)
    if getattr(request, "param", "legacy") == "native":
        # Native repository enrollment/readiness belongs to ISSUE-062. This
        # fixture exercises the real native publication boundary independently.
        from src.platform.project.readiness import ProjectReadiness

        admission.readiness = SimpleNamespace(
            resolve=lambda _: ProjectReadiness(project, True, True, True, "main")
        )
    service = AgentService(repo, admission)
    result = await asyncio.to_thread(
        service.submit,
        user,
        SubmitRun(
            project_id=project,
            agent_id=agent,
            request_id=str(uuid4()),
            prompt="Write the result file",
        ),
    )
    run = repo.rpc("claim", worker="integration")
    assert run["id"] == result["id"]
    value = SimpleNamespace(
        repo=repo,
        admission=admission,
        service=service,
        run=run,
        publication=Publication(container),
        checkpoints=Checkpoints(storage),
        ops=ops,
        user=user,
        project=project,
        agent=agent,
        postgres=postgres,
    )

    def supervisor(*, model=None, publication=None):
        return RunSupervisor(
            repo,
            admission,
            publication or value.publication,
            model or ModelFixture(),
            get_runtime_metering_service(),
            checkpoints=value.checkpoints,
            worker_factory=worker_factory,
        )

    value.supervisor = supervisor
    try:
        yield value
    finally:
        for execution in repo.executions(run["id"]):
            if execution["resource"]:
                await PiWorker.cleanup(execution["resource"])


async def approve_to_completion(case, task):
    async with asyncio.timeout(70):
        while not task.done():
            tools = await asyncio.to_thread(case.repo.tools, case.run["id"])
            for tool in tools:
                if tool["state"] == "waiting":
                    await asyncio.to_thread(
                        case.service.command,
                        case.user,
                        case.run["id"],
                        "approve",
                        call=tool["call_id"],
                        decision=str(uuid4()),
                        allow=True,
                    )
            await asyncio.sleep(0.1)
        await task
    return case.repo.get(case.run["id"])


async def wait_for_approval(case, task):
    async with asyncio.timeout(30):
        while not task.done():
            tools = await asyncio.to_thread(case.repo.tools, case.run["id"])
            if tools and tools[0]["state"] == "waiting":
                return tools[0]
            await asyncio.sleep(0.1)
        await task
        pytest.fail("Run ended before tool approval")


@pytest.mark.asyncio
async def test_real_run_publishes_and_survives_no_subscriber(prepared):
    case = prepared
    completed = await approve_to_completion(
        case, asyncio.create_task(case.supervisor().run_claim(case.run))
    )
    assert completed["state"] == "succeeded", completed
    assert case.ops.read_file(case.project, "result.txt") == b"durable result"
    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert any(e.get("message", {}).get("role") == "toolResult" for e in checkpoint["entries"])
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_publication_response_loss_keeps_original_material(prepared, after_commit):
    case = prepared

    class FailingPublisher:
        capture = case.publication.capture

        async def publish(self, *args):
            if after_commit:
                await case.publication.publish(*args)
            raise ConnectionError("Injected publication response loss")

    completed = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=FailingPublisher()).run_claim(case.run)),
    )
    assert completed["state"] == ("succeeded" if after_commit else "outcome_unknown")
    import base64

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert base64.b64decode(checkpoint["files"]["result.txt"]) == b"durable result"
    if after_commit:
        assert case.ops.read_file(case.project, "result.txt") == b"durable result"
    else:
        with pytest.raises(FileNotFoundError):
            case.ops.read_file(case.project, "result.txt")
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
async def test_approval_survives_destroyed_worker_and_new_execution(prepared):
    case = prepared
    first = case.supervisor()
    task = asyncio.create_task(first.run_claim(case.run))
    await wait_for_approval(case, task)
    task.cancel()  # process-loss simulation: no terminal ACK or cleanup
    with pytest.raises(asyncio.CancelledError):
        await task
    await first.worker.stop()
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    claimed = case.repo.rpc("claim", worker="replacement")
    assert claimed["execution_id"] != case.run["execution_id"]
    model = ModelFixture()
    model.calls = 1  # next deterministic provider response follows the restored tool
    replacement = case.supervisor(model=model)
    task = asyncio.create_task(replacement.run_claim(claimed))
    await asyncio.sleep(3)
    assert not task.done()
    assert case.repo.tools(claimed["id"])[0]["state"] == "waiting"
    completed = await approve_to_completion(case, task)
    assert completed["state"] == "succeeded", completed
    assert case.ops.read_file(case.project, "result.txt") == b"durable result"
    assert len(case.repo.tools(claimed["id"])) == 1


@pytest.mark.asyncio
async def test_stop_waiting_approval_has_no_tool_effect(prepared):
    case = prepared
    task = asyncio.create_task(case.supervisor().run_claim(case.run))
    await wait_for_approval(case, task)
    case.service.command(case.user, case.run["id"], "stop")
    await asyncio.wait_for(task, 30)
    completed = case.repo.get(case.run["id"])
    assert completed["state"] == "stopped", completed
    assert case.repo.tools(case.run["id"])[0]["state"] == "waiting"
    with pytest.raises(FileNotFoundError):
        case.ops.read_file(case.project, "result.txt")
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
async def test_completed_tool_receipt_recovers_before_manifest_ack(prepared, monkeypatch):
    case = prepared
    original = case.repo.write

    def crash(run, kind, payload=None, **patch):
        if kind == "checkpoint" and payload == {"reason": "after_tool"}:
            raise asyncio.CancelledError()
        return original(run, kind, payload, **patch)

    monkeypatch.setattr(case.repo, "write", crash)
    first = case.supervisor()
    with pytest.raises(asyncio.CancelledError):
        await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert case.repo.tools(case.run["id"])[0]["state"] == "completed"
    await first.worker.stop()
    monkeypatch.setattr(case.repo, "write", original)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    next_run = case.repo.rpc("claim", worker="receipt-recovery")
    model = ModelFixture()
    model.calls = 1
    await case.supervisor(model=model).run_claim(next_run)
    assert case.repo.get(case.run["id"])["state"] == "succeeded"
    assert case.ops.read_file(case.project, "result.txt") == b"durable result"
    assert len(case.repo.tools(case.run["id"])) == 1


@pytest.mark.asyncio
async def test_unknown_tool_is_not_replayed(prepared, monkeypatch):
    case = prepared
    original = case.repo.tool

    def crash(run, frame, state, result=None):
        if state == "completed":
            raise asyncio.CancelledError()
        return original(run, frame, state, result)

    monkeypatch.setattr(case.repo, "tool", crash)
    first = case.supervisor()
    with pytest.raises(asyncio.CancelledError):
        await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert case.repo.tools(case.run["id"])[0]["state"] == "executing"
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    next_run = case.repo.rpc("claim", worker="unknown-recovery")
    model = ModelFixture()
    await case.supervisor(model=model).run_claim(next_run)
    completed = case.repo.get(case.run["id"])
    assert completed["state"] == "outcome_unknown"
    assert model.calls == 0
    import base64

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert base64.b64decode(checkpoint["files"]["result.txt"]) == b"durable result"
    assert not case.repo.executions(case.run["id"])
    await first.worker.stop()


@pytest.mark.asyncio
async def test_checkpoint_outage_retains_workspace_until_retry(prepared, monkeypatch):
    case = prepared
    save = case.checkpoints.save
    outage = False

    async def unavailable(run, value):
        nonlocal outage
        if value["reason"] == "after_tool":
            outage = True
        if outage:
            raise ConnectionError("Injected object store outage")
        return await save(run, value)

    monkeypatch.setattr(case.checkpoints, "save", unavailable)
    first = case.supervisor()
    completed = await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert completed["state"] == "outcome_unknown"
    assert completed["snapshot"]["resource_retained"]
    assert case.repo.executions(case.run["id"])
    outage = False
    monkeypatch.setattr(case.checkpoints, "save", save)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    await case.supervisor().run_claim(case.repo.rpc("claim", worker="storage-recovered"))
    completed = case.repo.get(case.run["id"])
    assert completed["state"] == "outcome_unknown"
    assert not completed["snapshot"]["resource_retained"]
    import base64

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert base64.b64decode(checkpoint["files"]["result.txt"]) == b"durable result"
    assert not case.repo.executions(case.run["id"])
    await first.worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["revoke", "timeout", "model_failure"])
async def test_active_run_failures_settle_and_cleanup(prepared, cause):
    case = prepared
    model = ModelFixture()
    if cause == "model_failure":

        async def fail(*args):
            raise ConnectionError("Injected upstream failure")

        model.completion = fail
    task = asyncio.create_task(case.supervisor(model=model).run_claim(case.run))
    if cause != "model_failure":
        await wait_for_approval(case, task)
        if cause == "revoke":
            case.postgres.sql(f"UPDATE access_surfaces SET status='paused' WHERE id='{case.agent}'")
        else:
            case.postgres.sql(
                f"UPDATE agent_runs SET deadline=now()-interval '1 second' WHERE id='{case.run['id']}'"
            )
    await asyncio.wait_for(task, 30)
    assert case.repo.get(case.run["id"])["state"] == "failed"
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
async def test_cleanup_failure_retries_confirmed_publication(prepared, monkeypatch):
    case = prepared
    stop = PiWorker.stop

    async def unavailable(self):
        raise ConnectionError("Injected cleanup outage")

    monkeypatch.setattr(PiWorker, "stop", unavailable)
    first = case.supervisor()
    with pytest.raises(ConnectionError):
        await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert case.repo.get(case.run["id"])["publication"]["status"] == "committed"
    monkeypatch.setattr(PiWorker, "stop", stop)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    await case.supervisor().run_claim(case.repo.rpc("claim", worker="cleanup-recovered"))
    assert case.repo.get(case.run["id"])["state"] == "succeeded"
    assert not case.repo.executions(case.run["id"])
    await first.worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_native_canonical_publication_and_receipt(prepared):
    case = prepared
    completed = await approve_to_completion(
        case, asyncio.create_task(case.supervisor().run_claim(case.run))
    )
    assert completed["state"] == "succeeded", completed
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.read_file(case.project, "result.txt") == b"durable result"
    assert (
        case.postgres.sql(
            f"SELECT count(*) FROM version_ref_transactions WHERE request_key='{case.run['id']}'"
        )
        == "1"
    )
    assert (
        case.postgres.sql(f"SELECT count(*) FROM version_commits WHERE project_id='{case.project}'")
        == "0"
    )


@pytest.mark.asyncio
async def test_conflict_keeps_unpublished_files(prepared):
    case = prepared
    async with ProjectWriteLease(case.project, "fixture.base"):
        await case.ops.write_file(
            case.project,
            "result.txt",
            b"original result",
            who="user:" + case.user,
            source_channel="access_git",
        )

    class RacingPublisher:
        capture = case.publication.capture

        async def publish(self, run, checkpoint, grant):
            async with ProjectWriteLease(case.project, "fixture.concurrent"):
                await case.ops.write_file(
                    case.project,
                    "result.txt",
                    b"concurrent human content",
                    who="user:" + case.user,
                    source_channel="access_git",
                )
            return await case.publication.publish(run, checkpoint, grant)

    completed = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=RacingPublisher()).run_claim(case.run)),
    )
    assert completed["state"] == "conflict", completed
    assert case.ops.read_file(case.project, "result.txt") == b"concurrent human content"
    import base64

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert base64.b64decode(checkpoint["files"]["result.txt"]) == b"durable result"
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
async def test_runtime_credit_failure_before_compute(prepared):
    case = prepared
    supervisor = case.supervisor()

    class DeniedBilling:
        async def start_session(self, **kwargs):
            raise PermissionError("Runtime credits exhausted")

    supervisor.billing = DeniedBilling()
    await supervisor.run_claim(case.run)
    assert case.repo.get(case.run["id"])["state"] == "failed"
    assert supervisor.worker is None
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
async def test_model_stream_interruption_does_not_duplicate_partial_text(prepared):
    case = prepared

    class InterruptedModel:
        async def completion(self, *args):
            async def events():
                yield ModelChunk(
                    {
                        "id": "partial",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "partial prefix"},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                await asyncio.Event().wait()

            async def close():
                pass

            return InferenceRun(str(uuid4()), events(), close)

    first = case.supervisor(model=InterruptedModel())
    task = asyncio.create_task(first.run_claim(case.run))
    async with asyncio.timeout(20):
        while case.repo.get(case.run["id"])["snapshot"].get("text") != "partial prefix":
            assert not task.done()
            await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await first.worker.stop()
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    model = ModelFixture()
    model.calls = 1
    await case.supervisor(model=model).run_claim(case.repo.rpc("claim", worker="model-recovery"))
    completed = case.repo.get(case.run["id"])
    assert completed["state"] == "succeeded"
    assert completed["snapshot"]["text"] == "Saved."
    events = case.repo.events(case.run["id"], 0, completed["sequence"])
    assert any(event["kind"] == "text_reset" and event["payload"]["text"] == "" for event in events)
