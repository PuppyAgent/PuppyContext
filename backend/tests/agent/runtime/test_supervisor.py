"""Pi + PostgREST + PostgreSQL + MinIO + canonical publisher in one run."""

import asyncio
import base64
import copy
import json
from uuid import uuid4

import pytest

from src.platform.access.adapters.agent.runtime.admission import Admission
from src.platform.access.adapters.agent.runtime.checkpoints import Checkpoints
from src.platform.access.adapters.agent.runtime.models import SubmitRun
from src.platform.access.adapters.agent.runtime.publication import Publication
from src.platform.access.adapters.agent.runtime.repository import RunRepository
from src.platform.access.adapters.agent.runtime.runner import RunSupervisor
from src.platform.access.adapters.agent.runtime.workspace import SessionWorkspace
from src.platform.access.adapters.agent.service import AgentService
from src.platform.billing.runtime import get_runtime_metering_service
from src.platform.managed_ai.contracts import InferenceDone, InferenceRun, ModelChunk
from src.platform.project.write_lease import ProjectWriteLease
from src.platform.scope_sandbox.execution.pi_worker import PiWorker
from src.platform.scope_sandbox.execution.store import InMemoryExecutionSessionStore
from src.version_engine.adapters.git.run_transport import RunGitTransport
from tests.agent.runtime.workspace_assertions import recovery_file, recovery_git

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


class BashModel(ModelFixture):
    """Exercise real Pi bash and assert its result reaches the next model call."""

    def __init__(self, command):
        super().__init__()
        self.tool_id = "bash_" + uuid4().hex
        self.command = command + "\nprintf '\\nWORKSPACE_OPERATION_OK\\n'"

    async def completion(self, user, request, body):
        if self.calls:
            messages = body.model_dump()["messages"]
            last_result = [message for message in messages if message["role"] == "tool"][-1]
            assert "WORKSPACE_OPERATION_OK" in json.dumps(last_result), last_result
        result = await super().completion(user, request, body)

        async def events():
            async for event in result.events:
                if isinstance(event, ModelChunk):
                    for choice in event.frame.get("choices", []):
                        for call in choice.get("delta", {}).get("tool_calls", []):
                            call["id"] = self.tool_id
                            call["function"] = {
                                "name": "bash",
                                "arguments": json.dumps({"command": "set -eux\n" + self.command}),
                            }
                yield event

        return InferenceRun(str(uuid4()), events(), result.aclose)


class DestructiveModel(ModelFixture):
    """Require explicit confirmation before a reset and an observable effect."""

    async def completion(self, user, request, body):
        result = await super().completion(user, request, body)

        async def events():
            async for event in result.events:
                if isinstance(event, ModelChunk):
                    for choice in event.frame.get("choices", []):
                        for call in choice.get("delta", {}).get("tool_calls", []):
                            call["function"] = {
                                "name": "bash",
                                "arguments": json.dumps(
                                    {
                                        "command": "git reset --hard HEAD 2>/dev/null || true; "
                                        "printf 'durable result' > result.txt"
                                    }
                                ),
                            }
                yield event

        return InferenceRun(str(uuid4()), events(), result.aclose)


def read_case(case, path):
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        return reader.read_file(case.project, path)


def worker_factory(execution, project):
    return PiWorker(execution, project, provider="docker", store=InMemoryExecutionSessionStore())


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, 0, 2])
async def test_old_worker_protocol_rejected_before_model_or_tool(prepared, monkeypatch, version):
    original = PiWorker.receive

    async def incompatible(worker):
        frame = await original(worker)
        if frame["type"] == "ready":
            frame["operation_version"] = version
        return frame

    monkeypatch.setattr(PiWorker, "receive", incompatible)
    model = ModelFixture()
    await prepared.supervisor(model=model).run_claim(prepared.run)
    run = prepared.repo.get(prepared.run["id"])
    assert run["state"] == "failed", run
    assert run["snapshot"]["code"] == "worker_protocol_mismatch"
    assert model.calls == 0
    assert prepared.repo.tools(run["id"]) == []
    assert not prepared.repo.executions(run["id"])


@pytest.fixture
async def prepared(services, postgres, submitted, request):
    from types import SimpleNamespace

    client, _storage, container = services
    args, first = submitted
    postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE id='{first['id']}'")
    project, user, agent = args["project"], args["user"], args["agent"]
    ops = container.product_operations().for_user(project, user)
    profile = getattr(request, "param", "native")
    from tests.repository_hosting.integration.test_repository_file_policy import (
        enroll_file_policy,
    )
    from tests.repository_hosting.integration.test_repository_logical_billing import (
        enroll_billing,
    )

    org = postgres.row(f"SELECT org_id FROM projects WHERE id='{project}'")["org_id"]
    object_format = "sha256" if profile == "native_sha256" else "sha1"
    postgres.sql(f"""
        INSERT INTO version_repositories(project_id,authority,object_format) VALUES('{project}','native','{object_format}');
        INSERT INTO version_repository_refs(project_id,name,object_format,symbolic_target)
            VALUES('{project}',decode('48454144','hex'),'{object_format}',decode('726566732f68656164732f6d61696e','hex'));
        INSERT INTO version_organization_capacity(org_id,initialized) VALUES('{org}',true);
        INSERT INTO version_repository_capacity(project_id,org_id,initialized) VALUES('{project}','{org}',true);
    """)
    enrolled = enroll_billing(SimpleNamespace(pg=postgres, project=project, org=org))
    enroll_file_policy(enrolled, maximum=65536)
    postgres.sql(
        f"UPDATE organization_entitlements SET entitlements=jsonb_set(entitlements,'{{limits,storage.max_bytes}}','1048576'::jsonb) WHERE org_id='{org}'"
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
    if profile.startswith("native"):
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
        publication=Publication(RunGitTransport(container.repo_manager)),
        checkpoints=Checkpoints(),
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
            worker_lifecycle=PiWorker,
            workspace=SessionWorkspace(repo),
        )

    value.supervisor = supervisor
    try:
        yield value
    finally:
        # Multiple turns/sessions can allocate workers in a single test.
        workspaces = (
            client.table("agent_session_workspaces")
            .select("resource")
            .eq("project_id", project)
            .execute()
            .data
        )
        for workspace in workspaces:
            if workspace["resource"]:
                await PiWorker.cleanup(workspace["resource"])
        postgres.sql(
            f"UPDATE agent_session_workspaces SET state='retired',retire_after=NULL WHERE project_id='{project}'"
        )
        rows = client.table("agent_runs").select("id").eq("project_id", project).execute().data
        for row in rows:
            for execution in repo.executions(row["id"]):
                if execution["resource"]:
                    await PiWorker.cleanup(execution["resource"])


async def approve_to_completion(case, task, *, timeout=70):
    async with asyncio.timeout(timeout):
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
    assert read_case(case, "result.txt") == b"durable result"
    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert any(e.get("message", {}).get("role") == "toolResult" for e in checkpoint["entries"])
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_publication_response_loss_keeps_original_material(prepared, after_commit):
    case = prepared

    class FailingPublisher:
        capture = case.publication.capture
        exchange = case.publication.exchange

        async def publish(self, *args):
            if after_commit:
                await case.publication.publish(*args)
            raise ConnectionError("Injected publication response loss")

    completed = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=FailingPublisher()).run_claim(case.run)),
    )
    assert completed["state"] == ("succeeded" if after_commit else "outcome_unknown")

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert recovery_file(checkpoint, "result.txt") == b"durable result"
    if after_commit:
        assert read_case(case, "result.txt") == b"durable result"
    else:
        with pytest.raises(FileNotFoundError):
            read_case(case, "result.txt")
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_approval_survives_destroyed_worker_and_new_execution(prepared):
    case = prepared
    first = case.supervisor(model=DestructiveModel())
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
    model = DestructiveModel()
    model.calls = 1  # next deterministic provider response follows the restored tool
    replacement = case.supervisor(model=model)
    task = asyncio.create_task(replacement.run_claim(claimed))
    await asyncio.sleep(3)
    assert not task.done()
    assert case.repo.tools(claimed["id"])[0]["state"] == "waiting"
    completed = await approve_to_completion(case, task)
    assert completed["state"] == "succeeded", completed
    assert read_case(case, "result.txt") == b"durable result"
    assert len(case.repo.tools(claimed["id"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_stop_waiting_approval_has_no_tool_effect(prepared):
    case = prepared
    task = asyncio.create_task(case.supervisor(model=DestructiveModel()).run_claim(case.run))
    await wait_for_approval(case, task)
    case.service.command(case.user, case.run["id"], "stop")
    await asyncio.wait_for(task, 30)
    completed = case.repo.get(case.run["id"])
    assert completed["state"] == "stopped", completed
    assert case.repo.tools(case.run["id"])[0]["state"] == "waiting"
    with pytest.raises(FileNotFoundError):
        read_case(case, "result.txt")
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_completed_tool_receipt_recovers_before_manifest_ack(prepared, monkeypatch):
    case = prepared
    original = case.repo.complete_tool

    def crash(run, frame, *, result, checkpoint):
        original(run, frame, result=result, checkpoint=checkpoint)
        # SQL has saved receipt AND manifest; lose the entire command ACK.
        raise asyncio.CancelledError()

    monkeypatch.setattr(case.repo, "complete_tool", crash)
    first = case.supervisor()
    with pytest.raises(asyncio.CancelledError):
        await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert case.repo.tools(case.run["id"])[0]["state"] == "completed"
    await first.worker.stop()
    monkeypatch.setattr(case.repo, "complete_tool", original)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    next_run = case.repo.rpc("claim", worker="receipt-recovery")
    model = ModelFixture()
    model.calls = 1
    await case.supervisor(model=model).run_claim(next_run)
    assert case.repo.get(case.run["id"])["state"] == "succeeded"
    assert read_case(case, "result.txt") == b"durable result"
    assert len(case.repo.tools(case.run["id"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_unknown_tool_is_not_replayed(prepared, monkeypatch):
    case = prepared

    def crash(run, frame, *, result, checkpoint):
        # The effect ran, but no completion transaction reached the database.
        raise asyncio.CancelledError()

    monkeypatch.setattr(case.repo, "complete_tool", crash)
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

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert recovery_file(checkpoint, "result.txt") == b"durable result"
    assert not case.repo.executions(case.run["id"])
    await first.worker.stop()


@pytest.mark.asyncio
async def test_failed_provider_allocation_resolves_before_recovery(prepared, monkeypatch):
    case = prepared
    model = ModelFixture()
    supervisor = case.supervisor(model=model)
    supervisor.worker_factory = lambda execution, project: PiWorker(
        execution, project, provider="e2b"
    )
    resolved = []

    async def rejected(worker):
        raise RuntimeError("Template unavailable before provider allocation")

    async def absent(resource):
        resolved.append(resource)
        assert resource["project_id"] == case.project
        assert resource["allocation_pending"]
        return []

    monkeypatch.setattr(PiWorker, "create", rejected)
    monkeypatch.setattr(PiWorker, "resolve_resources", staticmethod(absent))
    await supervisor.run_claim(case.run)
    result = case.repo.get(case.run["id"])
    assert result["state"] == "failed"
    assert not result["snapshot"]["resource_retained"]
    assert not case.repo.executions(case.run["id"])
    assert model.calls == 0
    assert resolved
    checkpoint = await case.checkpoints.load(result, result["checkpoint"])
    assert checkpoint["reason"] == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
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

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert recovery_file(checkpoint, "result.txt") == b"durable result"
    assert not case.repo.executions(case.run["id"])
    await first.worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["revoke", "timeout", "model_failure"])
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_active_run_failures_settle_and_cleanup(prepared, cause):
    case = prepared
    model = DestructiveModel()
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
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_pause_failure_retries_confirmed_publication(prepared, monkeypatch):
    case = prepared
    pause = PiWorker.pause

    async def unavailable(self):
        raise ConnectionError("Injected cleanup outage")

    monkeypatch.setattr(PiWorker, "pause", unavailable)
    first = case.supervisor()
    with pytest.raises(ConnectionError):
        await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    assert case.repo.get(case.run["id"])["publication"]["status"] == "committed"
    monkeypatch.setattr(PiWorker, "pause", pause)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    await case.supervisor().run_claim(case.repo.rpc("claim", worker="cleanup-recovered"))
    assert case.repo.get(case.run["id"])["state"] == "succeeded"
    assert not case.repo.executions(case.run["id"])
    await first.worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_native_canonical_publication_and_receipt(prepared):
    case = prepared
    completed = await approve_to_completion(
        case, asyncio.create_task(case.supervisor().run_claim(case.run))
    )
    assert completed["state"] == "succeeded", completed
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.read_file(case.project, "result.txt") == b"durable result"
        cloud_tip = reader.get_head_commit_id(case.project)
    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert checkpoint["workspace"]["tip"] == cloud_tip
    assert "files" not in checkpoint and "git" not in checkpoint
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


async def native_write(case, name, content):
    from src.version_engine.adapters.product.tree_patch import splice_batch

    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        base = reader.get_read_revision(case.project)
    return await case.ops.apply_native_command(
        case.project,
        grant,
        request_key=str(uuid4()),
        base=base,
        input_sha256="b" * 64,
        splice=lambda store, tree: splice_batch(store, tree, [("put", name, content)]),
        message="Human knowledge edit",
    )


async def next_run(case, *, session_id=None):
    submitted = await asyncio.to_thread(
        case.service.submit,
        case.user,
        SubmitRun(
            project_id=case.project,
            agent_id=case.agent,
            session_id=session_id,
            request_id=str(uuid4()),
            prompt="Continue editing the knowledge repository",
        ),
    )
    claimed = case.repo.rpc("claim", worker="next-writing-turn")
    assert claimed["id"] == submitted["id"]
    return claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native", "native_sha256"], indirect=True)
async def test_native_multiple_writing_turns_preserve_files_modes_and_history(
    prepared, monkeypatch
):
    case = prepared
    create, pause, resume = PiWorker.create, PiWorker.pause, PiWorker.resume
    lifecycle = {"create": [], "pause": [], "resume": []}

    async def created(worker):
        result = await create(worker)
        lifecycle["create"].append(result["resource_id"])
        return result

    async def paused(worker):
        await pause(worker)
        lifecycle["pause"].append(worker.resource["resource_id"])

    async def resumed(worker, resource):
        result = await resume(worker, resource)
        lifecycle["resume"].append(result["resource_id"])
        assert worker.restore_point is None
        return result

    monkeypatch.setattr(PiWorker, "create", created)
    monkeypatch.setattr(PiWorker, "pause", paused)
    monkeypatch.setattr(PiWorker, "resume", resumed)
    await native_write(case, "draft.md", b"original human draft")
    await native_write(case, "obsolete.md", b"delete me")
    first_model = BashModel(
        "mv draft.md '章节 one.md'\n"
        "printf 'agent revision' >> '章节 one.md'\n"
        "rm obsolete.md\n"
        "node -e \"require('fs').writeFileSync('attachment.bin', Buffer.from([0,255,128,10,42]))\"\n"
        "printf '#!/bin/sh\\nexit 0\\n' > helper.sh\nchmod +x helper.sh\n"
        "printf '*.scratch\\n' > .gitignore\nprintf 'recovery only' > local.scratch"
    )
    first = await approve_to_completion(
        case, asyncio.create_task(case.supervisor(model=first_model).run_claim(case.run))
    )
    assert first["state"] == "succeeded", first
    saved = await case.checkpoints.load(first, first["checkpoint"])
    first_tip = saved["workspace"]["tip"]
    base_tip = saved["base"]["expected_oid"]
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.get_head_commit_id(case.project) == first_tip
        assert (
            reader.read_file(case.project, "章节 one.md") == b"original human draftagent revision"
        )
        assert reader.read_file(case.project, "attachment.bin") == bytes([0, 255, 128, 10, 42])
        assert reader.stat(case.project, "helper.sh").git_mode == "100755"
        for name in ("draft.md", "obsolete.md", "local.scratch"):
            assert reader.stat(case.project, name) is None
    assert recovery_file(saved, "local.scratch") == b"recovery only"
    case.run = await next_run(case, session_id=first["session_id"])
    second_model = BashModel(
        f'test "$(git rev-parse HEAD)" = "{first_tip}"\n'
        f'test "$(git rev-parse HEAD^)" = "{base_tip}"\n'
        f'test "$(git show {base_tip}:draft.md)" = "original human draft"\n'
        # Docker's workspace mount can deny execution independently of file bits.
        'test "$(stat -c %a helper.sh)" = 755\ntest ! -e obsolete.md\ntest -e local.scratch\n'
        "printf '\\nsecond turn' >> '章节 one.md'\nchmod -x helper.sh\nrm attachment.bin"
    )
    second = await approve_to_completion(
        case, asyncio.create_task(case.supervisor(model=second_model).run_claim(case.run))
    )
    assert second["state"] == "succeeded", second
    restored = await case.checkpoints.load(second, second["checkpoint"])
    second_tip = restored["workspace"]["tip"]
    assert restored["base"]["expected_oid"] == first_tip
    assert second_tip != first_tip
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.get_head_commit_id(case.project) == second_tip
        assert reader.read_file(case.project, "章节 one.md").endswith(b"\nsecond turn")
        assert reader.stat(case.project, "helper.sh").git_mode == "100644"
        assert reader.stat(case.project, "attachment.bin") is None
    case.run = await next_run(case, session_id=first["session_id"])
    third = await approve_to_completion(
        case,
        asyncio.create_task(
            case.supervisor(
                model=BashModel(
                    f'test "$(git rev-parse HEAD^)" = "{first_tip}"\n'
                    f'test "$(git rev-parse HEAD^^)" = "{base_tip}"\n'
                    'test "$(stat -c %a helper.sh)" = 644\ntest ! -e attachment.bin\ngit fsck --full'
                )
            ).run_claim(case.run)
        ),
    )
    assert third["state"] == "succeeded", third
    assert third["publication"]["status"] == "no_changes"
    final = await case.checkpoints.load(third, third["checkpoint"])
    assert final["workspace"]["tip"] == second_tip
    assert len(lifecycle["create"]) == 1
    assert lifecycle["pause"] == lifecycle["create"] * 3
    assert lifecycle["resume"] == lifecycle["create"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_two_native_agents_racing_preserve_winner_and_loser_commits(prepared):
    case = prepared
    await native_write(case, "result.txt", b"original")
    other = copy.copy(case)
    other.run = await next_run(case)
    ready = asyncio.Event()
    arrived = 0

    class RendezvousPublisher:
        capture = case.publication.capture
        exchange = case.publication.exchange

        async def publish(self, run, checkpoint, grant, worker):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 30)
            return await case.publication.publish(run, checkpoint, grant, worker)

    async def execute(target, content):
        return await approve_to_completion(
            target,
            asyncio.create_task(
                target.supervisor(
                    model=BashModel(f"printf '{content}' > result.txt"),
                    publication=RendezvousPublisher(),
                ).run_claim(target.run)
            ),
        )

    results = await asyncio.gather(execute(case, "writer one"), execute(other, "writer two"))
    assert sorted(result["state"] for result in results) == ["conflict", "succeeded"], results
    checkpoints = [await case.checkpoints.load(result, result["checkpoint"]) for result in results]
    assert checkpoints[0]["base"] == checkpoints[1]["base"]
    assert checkpoints[0]["workspace"]["tip"] != checkpoints[1]["workspace"]["tip"]
    for result, saved, content in zip(
        results, checkpoints, (b"writer one", b"writer two"), strict=True
    ):
        assert recovery_file(saved, "result.txt") == content
        if result["state"] == "succeeded":
            assert read_case(case, "result.txt") == content
            grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
            with case.ops.open_read(case.project, grant) as reader:
                assert reader.get_head_commit_id(case.project) == saved["workspace"]["tip"]
        assert not case.repo.executions(result["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
@pytest.mark.parametrize("fault", ["pack_checksum", "truncated_pack", "wrong_tip", "wrong_branch"])
async def test_native_invalid_git_publication_never_advances_cloud_branch(prepared, fault):
    case = prepared
    await native_write(case, "notes.md", b"original")
    attempted = None

    class CorruptPublisher:
        capture = case.publication.capture
        publish = case.publication.publish

        async def exchange(self, run, value, grant, frame):
            nonlocal attempted
            if frame["method"] == "POST" and frame["path"].endswith("/git-receive-pack"):
                attempted = copy.deepcopy(frame)
                data = bytearray(base64.b64decode(frame["body"]))
                if fault == "pack_checksum":
                    data[-1] ^= 0xFF
                elif fault == "truncated_pack":
                    data = data[:-32]
                elif fault == "wrong_tip":
                    data = data.replace(value["workspace"]["tip"].encode(), b"0" * 40, 1)
                else:
                    data = data.replace(b"refs/heads/main", b"refs/heads/evil", 1)
                frame = {**frame, "body": base64.b64encode(data).decode()}
            return await case.publication.exchange(run, value, grant, frame)

    result = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=CorruptPublisher()).run_claim(case.run)),
    )
    assert attempted is not None
    assert result["state"] in {"failed", "outcome_unknown"}, result
    assert not result["publication"]
    saved = await case.checkpoints.load(result, result["checkpoint"])
    assert recovery_file(saved, "result.txt") == b"durable result"
    assert recovery_git(saved, "rev-parse", "HEAD") == saved["workspace"]["tip"]
    recovery_git(saved, "fsck", "--full")
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.get_head_commit_id(case.project) == saved["base"]["expected_oid"]
        assert reader.read_file(case.project, "notes.md") == b"original"
        assert reader.stat(case.project, "result.txt") is None
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_native_rewritten_history_cannot_replace_existing_cloud_history(prepared):
    case = prepared
    await native_write(case, "notes.md", b"human history")
    model = BashModel(
        "git checkout --orphan unrelated\nprintf rewritten > notes.md\n"
        "git add -A\ngit commit -m 'unrelated root'\n"
        "git branch -f main HEAD\ngit checkout main"
    )
    result = await approve_to_completion(
        case, asyncio.create_task(case.supervisor(model=model).run_claim(case.run))
    )
    assert model.calls == 2
    assert result["state"] in {"failed", "outcome_unknown"}, result
    assert not result["publication"]
    saved = await case.checkpoints.load(result, result["checkpoint"])
    assert recovery_git(saved, "rev-parse", "HEAD") != saved["base"]["expected_oid"]
    assert recovery_file(saved, "notes.md") == b"rewritten"
    assert read_case(case, "notes.md") == b"human history"


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_native_history_and_non_main_branch_survive_next_turn(prepared):

    case = prepared
    case.postgres.sql(
        f"UPDATE version_repository_refs SET symbolic_target=convert_to('refs/heads/knowledge','UTF8') "
        f"WHERE project_id='{case.project}' AND name=convert_to('HEAD','UTF8')"
    )
    await native_write(case, "notes.md", b"first version")
    await native_write(case, "notes.md", b"second version")
    first = await approve_to_completion(
        case, asyncio.create_task(case.supervisor().run_claim(case.run))
    )
    assert first["state"] == "succeeded", first
    saved = await case.checkpoints.load(first, first["checkpoint"])
    assert saved["workspace"]["target_ref"] == "refs/heads/knowledge"

    class HistoryModel(ModelFixture):
        async def completion(self, user, request, body):
            if self.calls:
                assert "second version" in body.model_dump_json()
                assert "Human knowledge edit" in body.model_dump_json()
            result = await super().completion(user, request, body)

            async def events():
                async for event in result.events:
                    if isinstance(event, ModelChunk):
                        for choice in event.frame.get("choices", []):
                            for call in choice.get("delta", {}).get("tool_calls", []):
                                call["function"] = {
                                    "name": "bash",
                                    "arguments": json.dumps(
                                        {"command": "git log --format=%s; git show HEAD:notes.md"}
                                    ),
                                }
                    yield event

            return InferenceRun(
                result.request_id if hasattr(result, "request_id") else str(uuid4()),
                events(),
                result.aclose,
            )

    second = await asyncio.to_thread(
        case.service.submit,
        case.user,
        SubmitRun(
            project_id=case.project,
            agent_id=case.agent,
            session_id=first["session_id"],
            request_id=str(uuid4()),
            prompt="Read the saved writing history",
        ),
    )
    case.run = case.repo.rpc("claim", worker="history-second-turn")
    assert case.run["id"] == second["id"]
    completed = await approve_to_completion(
        case, asyncio.create_task(case.supervisor(model=HistoryModel()).run_claim(case.run))
    )
    assert completed["state"] == "succeeded", completed
    assert completed["publication"]["status"] == "no_changes"
    restored = await case.checkpoints.load(completed, completed["checkpoint"])
    assert restored["workspace"]["tip"] == saved["workspace"]["tip"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_native_git_race_keeps_original_commit_without_overwrite(prepared):
    case = prepared
    await native_write(case, "notes.md", b"original")

    class RacingPublisher:
        capture = case.publication.capture
        exchange = case.publication.exchange

        async def publish(self, run, checkpoint, grant, worker):
            await native_write(case, "notes.md", b"concurrent human edit")
            return await case.publication.publish(run, checkpoint, grant, worker)

    completed = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=RacingPublisher()).run_claim(case.run)),
    )
    assert completed["state"] == "conflict", completed
    saved = await case.checkpoints.load(completed, completed["checkpoint"])
    assert recovery_git(saved, "rev-parse", "HEAD") != saved["base"]["expected_oid"]
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    with case.ops.open_read(case.project, grant) as reader:
        assert reader.read_file(case.project, "notes.md") == b"concurrent human edit"
        assert reader.get_head_commit_id(case.project) != saved["workspace"]["tip"]
        assert reader.stat(case.project, "result.txt") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
@pytest.mark.parametrize("change", ["switch_head", "delete_branch"])
async def test_native_branch_selection_change_cannot_redirect_publication(prepared, change):
    case = prepared
    await native_write(case, "notes.md", b"original main branch")

    class ChangedBranchPublisher:
        capture = case.publication.capture
        exchange = case.publication.exchange

        async def publish(self, run, checkpoint, grant, worker):
            if change == "switch_head":
                case.postgres.sql(
                    "UPDATE version_repository_refs SET symbolic_target=convert_to('refs/heads/other','UTF8') "
                    f"WHERE project_id='{case.project}' AND name=convert_to('HEAD','UTF8')"
                )
            else:
                case.postgres.sql(
                    f"DELETE FROM version_repository_refs WHERE project_id='{case.project}' "
                    "AND name=convert_to('refs/heads/main','UTF8')"
                )
            return await case.publication.publish(run, checkpoint, grant, worker)

    result = await approve_to_completion(
        case,
        asyncio.create_task(
            case.supervisor(publication=ChangedBranchPublisher()).run_claim(case.run)
        ),
    )
    assert result["state"] == "conflict", result
    saved = await case.checkpoints.load(result, result["checkpoint"])
    assert saved["workspace"]["target_ref"] == "refs/heads/main"
    assert recovery_file(saved, "result.txt") == b"durable result"
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    metadata = await case.ops.native_ref_metadata(case.project, grant)
    refs = {base64.b64decode(ref["name_b64"]): ref for ref in metadata["refs"]}
    assert b"refs/heads/other" not in refs
    if change == "switch_head":
        assert refs[b"refs/heads/main"]["state"]["oid"] == saved["base"]["expected_oid"]
    else:
        assert b"refs/heads/main" not in refs


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
@pytest.mark.parametrize(
    "restriction",
    [
        {"excludes": ["private"]},
        {"path_prefix": "public"},
        {"materialize": False},
        {"scope_id": "scope"},
    ],
)
async def test_native_restricted_agent_never_receives_full_history(prepared, restriction):
    case = prepared
    grant = case.admission.authorization.resolve_project_grant(case.project, case.user)
    restricted = {**case.run, "policy": {**case.run["policy"], **restriction}}
    if "scope_id" in restriction:
        restricted["scope_id"] = restriction["scope_id"]
    with pytest.raises(ValueError, match="unrestricted Project-root"):
        case.publication.capture(restricted, grant)


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
@pytest.mark.parametrize("cause", ["stop", "timeout", "revoke"])
async def test_native_revocation_at_publication_cannot_advance_ref(prepared, cause):
    case = prepared

    class StoppedPublisher:
        capture = case.publication.capture
        exchange = case.publication.exchange

        async def publish(self, run, checkpoint, grant, worker):
            if cause == "stop":
                case.service.command(case.user, run["id"], "stop")
            elif cause == "timeout":
                case.postgres.sql(
                    f"UPDATE agent_runs SET deadline=now()-interval '1 second' WHERE id='{run['id']}'"
                )
            else:
                case.postgres.sql(
                    f"UPDATE access_surfaces SET status='paused' WHERE id='{case.agent}'"
                )
            return await case.publication.publish(run, checkpoint, grant, worker)

    completed = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=StoppedPublisher()).run_claim(case.run)),
    )
    assert completed["state"] != "succeeded"
    assert not completed["publication"]
    with pytest.raises(FileNotFoundError):
        read_case(case, "result.txt")
    saved = await case.checkpoints.load(completed, completed["checkpoint"])
    assert recovery_git(saved, "rev-parse", "HEAD")


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
@pytest.mark.parametrize("phase", ["agent_settled", "settled"])
async def test_native_settled_model_recovers_without_running_model_again(
    prepared, monkeypatch, phase
):
    case = prepared
    method = "settle_model" if phase == "agent_settled" else "write"
    original = getattr(case.repo, method)

    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        if phase == "agent_settled" or (args[1:3] == ("checkpoint", {"reason": phase})):
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(case.repo, method, crash)
    first = case.supervisor()
    with pytest.raises(asyncio.CancelledError):
        await approve_to_completion(case, asyncio.create_task(first.run_claim(case.run)))
    before = case.repo.get(case.run["id"])
    saved = await case.checkpoints.load(before, before["checkpoint"])
    assert saved["reason"] == phase
    await first.worker.stop()
    monkeypatch.setattr(case.repo, method, original)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    model = ModelFixture()
    await case.supervisor(model=model).run_claim(case.repo.rpc("claim", worker="settled-recovery"))
    assert model.calls == 0
    assert case.repo.get(case.run["id"])["state"] == "succeeded"
    assert read_case(case, "result.txt") == b"durable result"
    if phase == "settled":
        result = case.repo.get(case.run["id"])
        final = await case.checkpoints.load(result, result["checkpoint"])
        assert final["workspace"]["tip"] == saved["workspace"]["tip"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_native_storage_outage_after_commit_retains_exact_commit_until_durable(
    prepared, monkeypatch
):
    case = prepared
    save = case.checkpoints.save
    committed_tip = None

    async def unavailable(run, value):
        nonlocal committed_tip
        if value["reason"] == "settled":
            committed_tip = value["workspace"]["tip"]
        if committed_tip:
            assert value["workspace"]["tip"] == committed_tip
            raise ConnectionError("Injected storage loss after sandbox commit")
        return await save(run, value)

    monkeypatch.setattr(case.checkpoints, "save", unavailable)
    result = await approve_to_completion(
        case, asyncio.create_task(case.supervisor().run_claim(case.run))
    )
    assert committed_tip
    assert result["state"] == "failed", result
    assert result["snapshot"]["resource_retained"]
    assert case.repo.executions(case.run["id"])
    with pytest.raises(FileNotFoundError):
        read_case(case, "result.txt")
    monkeypatch.setattr(case.checkpoints, "save", save)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    model = ModelFixture()
    await case.supervisor(model=model).run_claim(case.repo.rpc("claim", worker="storage-returned"))
    result = case.repo.get(case.run["id"])
    assert result["state"] == "failed"
    assert not result["publication"]
    assert not result["snapshot"]["resource_retained"]
    saved = await case.checkpoints.load(result, result["checkpoint"])
    assert saved["workspace"]["tip"] == committed_tip
    assert recovery_file(saved, "result.txt") == b"durable result"
    assert model.calls == 0
    assert not case.repo.executions(case.run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", ["native"], indirect=True)
async def test_native_model_error_checkpoint_never_becomes_success_after_crash(
    prepared, monkeypatch
):
    case = prepared
    original = case.repo.write

    def crash(run, kind, payload=None, **patch):
        result = original(run, kind, payload, **patch)
        if kind == "checkpoint" and payload == {"reason": "model_failed"}:
            raise asyncio.CancelledError()
        return result

    async def failed_model(*args):
        raise ConnectionError("Injected model failure")

    model = ModelFixture()
    model.completion = failed_model
    monkeypatch.setattr(case.repo, "write", crash)
    first = case.supervisor(model=model)
    with pytest.raises(asyncio.CancelledError):
        await first.run_claim(case.run)
    await first.worker.stop()
    monkeypatch.setattr(case.repo, "write", original)
    case.postgres.sql(
        f"UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id='{case.run['id']}'"
    )
    replacement = ModelFixture()
    await case.supervisor(model=replacement).run_claim(
        case.repo.rpc("claim", worker="error-recovery")
    )
    result = case.repo.get(case.run["id"])
    assert result["state"] == "failed", result
    assert not result["publication"]
    assert replacement.calls == 0


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
        exchange = case.publication.exchange

        async def publish(self, run, checkpoint, grant, worker):
            async with ProjectWriteLease(case.project, "fixture.concurrent"):
                await case.ops.write_file(
                    case.project,
                    "result.txt",
                    b"concurrent human content",
                    who="user:" + case.user,
                    source_channel="access_git",
                )
            return await case.publication.publish(run, checkpoint, grant, worker)

    completed = await approve_to_completion(
        case,
        asyncio.create_task(case.supervisor(publication=RacingPublisher()).run_claim(case.run)),
    )
    assert completed["state"] == "conflict", completed
    assert read_case(case, "result.txt") == b"concurrent human content"

    checkpoint = await case.checkpoints.load(completed, completed["checkpoint"])
    assert recovery_file(checkpoint, "result.txt") == b"durable result"
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


@pytest.mark.asyncio
async def test_detached_writers_cannot_cross_confirmed_tool_boundary(prepared):
    case = prepared
    model = BashModel(
        "(sleep 2; echo late > late.txt) >background.log 2>&1 &\nprintf saved > note.md"
    )
    original = model.completion

    async def delayed(*args):
        if model.calls:
            await asyncio.sleep(3)
        return await original(*args)

    model.completion = delayed
    result = await approve_to_completion(
        case, asyncio.create_task(case.supervisor(model=model).run_claim(case.run))
    )
    assert result["state"] == "succeeded", result
    assert read_case(case, "note.md") == b"saved"
    with pytest.raises(FileNotFoundError):
        read_case(case, "late.txt")
