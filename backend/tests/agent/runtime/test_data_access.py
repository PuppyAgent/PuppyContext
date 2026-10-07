"""Real transport budgets and authority races; no mocked database repositories."""

import asyncio
import concurrent.futures
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.exceptions import DatabaseSchemaOutdatedException, NotFoundException
from src.infra.supabase.instrumentation import DatabaseTrace, database_trace
from src.platform.access.adapters.agent.runtime.models import SubmitRun
from tests.agent.runtime.test_supervisor import ModelFixture
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture

prepared = prepared_fixture
pytestmark = pytest.mark.integration


def measured(name, call):
    trace = DatabaseTrace(name)
    with database_trace(trace):
        value = call()
    return value, trace.report()


@pytest.mark.asyncio
async def test_named_operations_have_real_http_budgets(prepared):
    c = prepared
    context, report = measured("context", lambda: c.admission.load(c.user, c.project, c.agent))
    assert report["attempts"] == 1, report
    assert report["max_inflight"] == 1
    assert context.grant.project_id == c.project
    for call in [
        lambda: c.service.snapshot(c.user, c.run["id"]),
        lambda: c.repo.renew_execution(c.run),
        lambda: c.repo.load_execution(c.run["id"]),
    ]:
        _, report = measured("operation", call)
        assert report["attempts"] == 1, report
    c.postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE id='{c.run['id']}'")
    request = SubmitRun(
        project_id=c.project, agent_id=c.agent, request_id=uuid4(), prompt="Budget reply"
    )
    run, report = measured("submit", lambda: c.service.submit(c.user, request))
    assert report["attempts"] == 2, report
    same, report = measured("replay", lambda: c.service.submit(c.user, request))
    assert same["id"] == run["id"]
    assert report["attempts"] == 1, report
    claim, report = measured("claim", lambda: c.repo.claim_run(worker="budget"))
    assert claim["id"] == run["id"]
    assert report["attempts"] == 1, report


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["org_member", "project_member", "visibility", "lifecycle", "config", "paused"]
)
async def test_renewal_rejects_changed_authority_in_one_request(prepared, change):
    c = prepared
    if change == "project_member":
        import json

        member = str(uuid4())
        org = c.postgres.row(f"SELECT org_id FROM projects WHERE id='{c.project}'")["org_id"]
        c.postgres.sql(
            f"INSERT INTO auth.users(id) VALUES('{member}'); INSERT INTO org_members(org_id,user_id,role) VALUES('{org}','{member}','member'); INSERT INTO project_members(project_id,org_id,user_id,role) VALUES('{c.project}','{org}','{member}','editor')"
        )
        context = c.admission.load(member, c.project, c.agent)
        policy = json.dumps(context.policy).replace("'", "''")
        c.postgres.sql(
            f"UPDATE agent_runs SET user_id='{member}',policy='{policy}'::jsonb WHERE id='{c.run['id']}'"
        )
        c.run = c.repo.get(c.run["id"])
    statements = {
        "org_member": f"UPDATE org_members SET role='member' WHERE user_id='{c.user}'",
        "project_member": f"UPDATE project_members SET role='viewer' WHERE project_id='{c.project}' AND user_id='{member if change == 'project_member' else c.user}'",
        "visibility": f"UPDATE projects SET visibility=CASE WHEN visibility='org' THEN 'private' ELSE 'org' END WHERE id='{c.project}'",
        "lifecycle": f"UPDATE projects SET lifecycle_status='deleting' WHERE id='{c.project}'",
        "config": f"UPDATE access_surfaces SET config=config||'{{\"system_prompt\":\"changed\"}}' WHERE id='{c.agent}'",
        "paused": f"UPDATE access_surfaces SET status='paused' WHERE id='{c.agent}'",
    }
    c.postgres.sql(statements[change])
    value, report = measured("revoke", lambda: c.repo.renew_execution(c.run))
    assert value["code"] == "authorization_revoked", (change, value)
    assert report["attempts"] == 1


@pytest.mark.asyncio
async def test_submit_rejects_stale_context_and_isolation(prepared):
    c = prepared
    context = c.admission.load(c.user, c.project, c.agent)
    c.postgres.sql(
        f"UPDATE access_surfaces SET config=config||'{{\"visibility\":\"private\"}}' WHERE id='{c.agent}'"
    )
    with pytest.raises(HTTPException) as error:
        c.repo.submit_run(
            user=c.user,
            project=c.project,
            agent=c.agent,
            session=None,
            request=str(uuid4()),
            digest="b" * 64,
            prompt="stale",
            policy=context.policy,
            timeout=600,
        )
    assert error.value.status_code == 409
    assert c.repo.load_run_view(str(uuid4()), c.run["id"]) is None
    assert (
        c.postgres.sql(
            f"SELECT count(*) FROM agent_runs WHERE project_id='{c.project}' AND prompt='stale'"
        )
        == "0"
    )


@pytest.mark.asyncio
async def test_renew_does_not_call_admission_or_configuration(prepared, monkeypatch):
    c = prepared

    def forbidden(*args, **kwargs):
        raise AssertionError("renewal must not perform full admission")

    monkeypatch.setattr(c.admission, "recheck", forbidden)
    supervisor = c.supervisor()
    supervisor.run = c.run
    supervisor.context = c.admission.load(c.user, c.project, c.agent)
    trace = DatabaseTrace("renewal")
    with database_trace(trace):
        await supervisor.check()
    assert trace.attempts == 1


@pytest.mark.asyncio
async def test_real_database_concurrent_views_are_counted(prepared):
    c = prepared
    trace = DatabaseTrace("concurrent")
    with database_trace(trace):
        values = await asyncio.gather(
            *[asyncio.to_thread(c.service.snapshot, c.user, c.run["id"]) for _ in range(8)]
        )
    assert {v["id"] for v in values} == {c.run["id"]}
    assert trace.attempts == 8
    assert 2 <= trace.max_inflight <= 8
    assert trace.inflight == 0 and trace.failures == 0


@pytest.mark.asyncio
async def test_simultaneous_idempotent_submissions_and_session_conflict(prepared):
    c = prepared
    c.postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE id='{c.run['id']}'")
    request = SubmitRun(
        project_id=c.project, agent_id=c.agent, request_id=uuid4(), prompt="One reply"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: c.service.submit(c.user, request), range(8)))
    assert len({r["id"] for r in results}) == 1
    with pytest.raises(HTTPException) as error:
        c.service.submit(
            c.user,
            request.model_copy(
                update={"request_id": uuid4(), "session_id": results[0]["session_id"]}
            ),
        )
    assert error.value.status_code == 409


class FragmentedModel(ModelFixture):
    def __init__(self):
        super().__init__()
        self.calls = 1
        self.expected = "这是完整回复。\n" * 70

    async def completion(self, *args):
        from src.platform.managed_ai.contracts import InferenceRun, ModelChunk

        value = await super().completion(*args)

        async def events():
            async for event in value.events:
                if isinstance(event, ModelChunk) and event.frame["choices"][0]["delta"].get(
                    "content"
                ):
                    for text in ["这是完整回复。\n"] * 70:
                        frame = {
                            **event.frame,
                            "choices": [
                                {"index": 0, "delta": {"content": text}, "finish_reason": None}
                            ],
                        }
                        yield ModelChunk(frame)
                else:
                    yield event

        return InferenceRun(str(uuid4()), events(), value.aclose)


@pytest.mark.asyncio
async def test_real_pi_fragmented_reply_and_control_budget(prepared, tmp_path):
    import json

    c = prepared
    model = FragmentedModel()
    supervisor = c.supervisor(model=model)
    await supervisor.run_claim(c.run)
    run = c.repo.get(c.run["id"])
    assert run["state"] == "succeeded", run
    assert run["snapshot"]["text"] == model.expected
    events = c.repo.events(run["id"], 0, run["sequence"])
    assert "".join(e["payload"]["delta"] for e in events if e["kind"] == "text") == model.expected
    report = supervisor.metrics.report()
    report.update(
        elapsed_seconds=supervisor.elapsed_seconds, first_text_seconds=supervisor.first_text_seconds
    )
    assert report["operations"].get("POST rpc/agent_run_append_batch", 0) <= 2, report
    control = sum(count for name, count in report["operations"].items() if "agent_run" in name)
    assert control <= 20, report
    assert report["max_inflight"] <= 2, report
    (tmp_path / "reply-performance.json").write_text(json.dumps(report, indent=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 100])
async def test_workspace_metadata_queries_do_not_scale_per_file(prepared, count, tmp_path):
    import base64

    from src.platform.project.write_lease import ProjectWriteLease

    c = prepared
    files = {f"notes/{i}.md": f"Knowledge document {i}".encode() for i in range(count)}
    async with ProjectWriteLease(c.project, "fixture.batch"):
        await c.ops.bulk_write(c.project, files, who="user:" + c.user, source_channel="access_git")
    grant = c.admission.load(c.user, c.project, c.agent).grant
    value, report = measured("workspace", lambda: c.publication.capture(c.run, grant))
    assert {name: base64.b64decode(data) for name, data in value["files"].items()} == files
    assert value["git"]["tip"] and value["git"]["bundle"]
    assert report["attempts"] <= 4, report
    assert report["operations"].get("POST rpc/get_version_pinned_object_locations") == 1
    assert not any("version_object_locations" in name for name in report["operations"])
    import json

    (tmp_path / "workspace-performance.json").write_text(
        json.dumps({"files": count, **report}, indent=2)
    )


@pytest.mark.asyncio
async def test_renewal_invalidates_tool_bindings_and_definition_changes(prepared):
    c = prepared
    org = c.postgres.row(f"SELECT org_id FROM projects WHERE id='{c.project}'")["org_id"]
    tool = str(uuid4())
    c.postgres.sql(
        f"INSERT INTO tools(id,project_id,org_id,type,name,path) VALUES('{tool}','{c.project}','{org}','search','Knowledge','notes')"
    )
    import json

    for mutation in [
        f"INSERT INTO access_tools(access_surface_id,tool_id) VALUES('{c.agent}','{tool}')",
        f"UPDATE access_tools SET enabled=false WHERE access_surface_id='{c.agent}'",
        f"UPDATE tools SET path='changed' WHERE id='{tool}'",
        f"DELETE FROM access_tools WHERE access_surface_id='{c.agent}'",
    ]:
        context = c.admission.load(c.user, c.project, c.agent)
        policy = json.dumps(context.policy).replace("'", "''")
        c.postgres.sql(f"UPDATE agent_runs SET policy='{policy}'::jsonb WHERE id='{c.run['id']}'")
        c.postgres.sql(mutation)
        result = c.repo.renew_execution(c.run)
        assert result["code"] == "authorization_revoked"


@pytest.mark.asyncio
async def test_ten_second_model_reply_has_bounded_renewal(prepared, tmp_path):
    c = prepared

    class DelayedModel(FragmentedModel):
        async def completion(self, *args):
            await asyncio.sleep(10)
            return await super().completion(*args)

    model = DelayedModel()
    supervisor = c.supervisor(model=model)
    await supervisor.run_claim(c.run)
    result = c.service.snapshot(c.user, c.run["id"])
    assert result["state"] == "succeeded"
    assert result["snapshot"]["text"] == model.expected
    report = supervisor.metrics.report()
    control = sum(count for name, count in report["operations"].items() if "agent_run" in name)
    assert control <= 20, report
    assert report["max_inflight"] <= 2
    assert supervisor.first_text_seconds >= 10
    assert supervisor.elapsed_seconds < 30
    import json

    report.update(
        elapsed_seconds=supervisor.elapsed_seconds, first_text_seconds=supervisor.first_text_seconds
    )
    (tmp_path / "delayed-reply-performance.json").write_text(json.dumps(report, indent=2))


@pytest.mark.asyncio
async def test_database_function_acl_denies_browser_roles(prepared):
    c = prepared
    for role in ("anon", "authenticated"):
        for function in (
            "agent_run_context(uuid,text,text,text,uuid)",
            "agent_run_renew(uuid,uuid,bigint)",
            "authorization_project_facts(text,uuid)",
            "get_version_pinned_object_locations(text,text,uuid)",
        ):
            assert (
                c.postgres.sql(
                    f"SELECT has_function_privilege('{role}','public.{function}','EXECUTE')"
                )
                == "f"
            )


@pytest.mark.asyncio
async def test_pinned_bulk_reader_cannot_outlive_pin_or_cross_project(prepared):
    c = prepared
    grant = c.admission.load(c.user, c.project, c.agent).grant
    with c.ops.open_read(c.project, grant, bulk=True) as reader:
        backend = reader.snapshot.backend
        pin = reader.snapshot.pin
        with pytest.raises(Exception, match="repository_read_pin_unavailable"):
            reader.snapshot.control.call(
                "get_version_pinned_object_locations",
                p_project_id=str(uuid4()),
                p_actor=reader.snapshot.actor,
                p_pin_id=pin,
            )
    with pytest.raises(RuntimeError, match="closed"):
        backend.get_durable("a" * 40)


class FileQuestionModel(ModelFixture):
    """A deterministic model adapter that validates actual Pi tool feedback."""

    expected = (
        "共有 3 个文件：alpha.md、beta.md、gamma.md。alpha.md 的内容是：first knowledge note。"
    )

    async def completion(self, user, request, body):
        from src.platform.managed_ai.contracts import InferenceRun, ModelChunk

        if self.calls:
            results = [
                message for message in body.model_dump()["messages"] if message["role"] == "tool"
            ]
            import json

            content = json.dumps(results[-1], ensure_ascii=False)
            if self.calls == 1:
                assert all(name in content for name in ("alpha.md", "beta.md", "gamma.md")), content
                assert "fd is not available" not in content
            elif self.calls == 2:
                assert "first knowledge note" in content
        result = await super().completion(user, request, body)
        call = self.calls

        async def events():
            async for event in result.events:
                if isinstance(event, ModelChunk):
                    frame = event.frame
                    if frame["choices"][0]["finish_reason"] is None:
                        import json

                        delta = (
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"file_question_{call}",
                                        "type": "function",
                                        "function": {
                                            "name": "find" if call == 1 else "read",
                                            "arguments": json.dumps(
                                                {"pattern": "*.md", "path": "."}
                                                if call == 1
                                                else {"path": "alpha.md"}
                                            ),
                                        },
                                    }
                                ],
                            }
                            if call < 3
                            else {"role": "assistant", "content": self.expected}
                        )
                        frame["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
                    else:
                        frame["choices"][0]["finish_reason"] = "tool_calls" if call < 3 else "stop"
                    yield ModelChunk(frame)
                else:
                    yield event

        return InferenceRun(str(uuid4()), events(), result.aclose)


@pytest.mark.asyncio
async def test_real_pi_answers_file_question_using_offline_find_and_read(prepared):
    from src.platform.project.write_lease import ProjectWriteLease

    c = prepared
    files = {"alpha.md": b"first knowledge note", "beta.md": b"second", "gamma.md": b"third"}
    async with ProjectWriteLease(c.project, "fixture.question"):
        await c.ops.bulk_write(c.project, files, who="user:" + c.user, source_channel="access_git")
    model = FileQuestionModel()
    supervisor = c.supervisor(model=model)
    await supervisor.run_claim(c.run)
    result = c.service.snapshot(c.user, c.run["id"])
    assert result["state"] == "succeeded", result
    assert result["snapshot"]["text"] == model.expected
    assert model.calls == 3
    tools = c.repo.tools(c.run["id"])
    assert {tool["name"] for tool in tools} == {"find", "read"}
    assert all(tool["state"] == "completed" for tool in tools)
    assert result["publication"]["status"] == "no_changes"


@pytest.mark.asyncio
async def test_batch_identity_handles_lost_response_without_duplicate_delta(prepared, monkeypatch):
    c = prepared
    supervisor = c.supervisor()
    supervisor.run = c.run
    supervisor.pending_text = "唯一的一段回复"
    original = c.repo.append_events_batch
    calls = 0

    def lose_response(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            raise ConnectionError("Committed response lost")
        return result

    monkeypatch.setattr(c.repo, "append_events_batch", lose_response)
    with pytest.raises(ConnectionError):
        await supervisor.flush_text()
    supervisor.run = c.repo.get(c.run["id"])
    await supervisor.flush_text()
    run = c.repo.get(c.run["id"])
    assert run["snapshot"]["text"] == "唯一的一段回复"
    texts = [
        event for event in c.repo.events(run["id"], 0, run["sequence"]) if event["kind"] == "text"
    ]
    assert len(texts) == 1
    assert supervisor.pending_text == "" and supervisor.pending_batch is None


@pytest.mark.asyncio
async def test_large_unicode_response_is_split_without_loss(prepared):
    c = prepared
    supervisor = c.supervisor()
    supervisor.run = c.run
    text = "医学知识与写作📚\n" * 21000
    supervisor.pending_text = text
    await supervisor.flush_text()
    run = c.repo.get(c.run["id"])
    assert run["snapshot"]["text"] == text[-200000:]
    events = c.repo.events(run["id"], 0, run["sequence"])
    assert "".join(event["payload"]["delta"] for event in events if event["kind"] == "text") == text
    assert len(events) < 20


@pytest.mark.asyncio
async def test_revision_guard_serializes_concurrent_revocation(prepared):
    """A revocation cannot commit inside a previously admitted command."""
    import subprocess
    import threading

    from tests.agent.runtime.conftest import PSQL, literal

    c = prepared
    token = c.admission.load(c.user, c.project, c.agent).policy["revision"]
    process = subprocess.Popen(
        [PSQL, c.postgres.url, "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        process.stdin.write(
            f"BEGIN; SELECT authorization_assert_revision({literal(c.project)},{literal(token)}); SELECT 'LOCKED';\n"
        )
        process.stdin.flush()
        while process.stdout.readline().strip() != "LOCKED":
            assert process.poll() is None
        completed = threading.Event()

        def revoke():
            c.postgres.sql(f"UPDATE access_surfaces SET status='paused' WHERE id='{c.agent}'")
            completed.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(revoke)
            assert not completed.wait(0.2), "revocation must wait for the admitted transaction"
            process.stdin.write("COMMIT;\n\\q\n")
            process.stdin.flush()
            future.result(timeout=10)
        assert process.wait(timeout=5) == 0
        assert c.repo.renew_execution(c.run)["code"] == "authorization_revoked"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_history_has_one_query_independent_of_session_count_and_rechecks_access(prepared):
    c = prepared
    c.postgres.sql(
        f"DELETE FROM chat_sessions WHERE agent_id='{c.agent}' AND id<>'{c.run['session_id']}'"
    )
    for count in (1, 100):
        if count > 1:
            c.postgres.sql(f"""
                INSERT INTO chat_sessions(id,user_id,agent_id,mode,title)
                SELECT gen_random_uuid()::text,'{c.user}','{c.agent}','cloud_pi','history'
                FROM generate_series(1,99);
            """)
        trace = DatabaseTrace("history")
        with database_trace(trace):
            rows = await asyncio.to_thread(c.service.history, c.user, c.project, c.agent, 200)
        assert len(rows) == count
        assert trace.attempts == 1
        assert trace.operations == {"POST rpc/agent_run_history_view": 1}
    with pytest.raises(HTTPException) as wrong_project:
        c.service.history(c.user, str(uuid4()), c.agent)
    assert wrong_project.value.status_code == 404
    with pytest.raises(NotFoundException):
        c.service.history(str(uuid4()), c.project, c.agent)
    c.postgres.sql(f"UPDATE projects SET lifecycle_status='deleting' WHERE id='{c.project}'")
    with pytest.raises(NotFoundException):
        c.service.history(c.user, c.project, c.agent)


@pytest.mark.asyncio
async def test_missing_database_contract_is_retryable_and_sanitized(prepared):
    trace = DatabaseTrace("schema-version-mismatch")
    with database_trace(trace), pytest.raises(DatabaseSchemaOutdatedException) as error:
        await asyncio.to_thread(prepared.repo.rpc, "missing_contract_fixture")
    assert trace.attempts == trace.failures == 1
    assert error.value.status_code == 503
    assert error.value.details == {"code": "database_schema_outdated", "retryable": True}
    assert "missing_contract_fixture" not in str(error.value)


@pytest.mark.asyncio
async def test_billing_revocation_during_model_wait_is_still_supervised(prepared):
    from src.platform.managed_ai.contracts import InferenceRun, ModelChunk

    started = asyncio.Event()

    class WaitingModel:
        async def completion(self, *args):
            async def events():
                started.set()
                await asyncio.Event().wait()
                yield ModelChunk({})

            async def close():
                pass

            return InferenceRun(str(uuid4()), events(), close)

    class Billing:
        async def start_session(self, **kwargs):
            return "owned-billing-fixture"

        async def check_session(self, identity):
            if started.is_set():
                raise PermissionError("credits revoked while the model is waiting")

        async def finish_session(self, identity):
            pass

    supervisor = prepared.supervisor(model=WaitingModel())
    supervisor.billing = Billing()
    await asyncio.wait_for(supervisor.run_claim(prepared.run), timeout=20)
    current = prepared.repo.get(prepared.run["id"])
    assert started.is_set()
    assert current["state"] == "failed"
    assert current["snapshot"]["code"] == "runtime_credit_unavailable"
