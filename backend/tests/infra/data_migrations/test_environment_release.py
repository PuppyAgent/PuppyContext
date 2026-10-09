"""Release safety tests exercise ordering, persisted outcomes and provider I/O."""

from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import httpx
import pytest

from src.infra.data_migrations.release.backup import wait_until_ready
from src.infra.data_migrations.release.engine import Release
from src.infra.data_migrations.release.hosted import ROLES, Hosted
from src.infra.data_migrations.release.plan import Phase, ReleasePlan
from src.infra.data_migrations.release.railway import Railway
from src.infra.data_migrations.release.target import DatabaseTarget

ROOT = Path(__file__).resolve().parents[4]
SOURCE = "a" * 40
PLAN = ReleasePlan(
    (
        Phase("expand", "schema", "1"),
        Phase("copy", "data", "copy"),
        Phase("contract", "schema", "2"),
    ),
    "checksum",
    frozenset({"1", "2"}),
)


def test_restore_waits_past_the_temporary_init_server(monkeypatch, tmp_path):
    sleeps = []

    def readiness(command, **kwargs):
        # The Unix socket accepts connections throughout initialization, but
        # that server will be stopped before the final TCP server starts.
        final_server = "-h" in command and command[command.index("-h") + 1] == "127.0.0.1"
        return SimpleNamespace(returncode=0 if not final_server or len(sleeps) >= 2 else 1)

    monkeypatch.setattr("src.infra.data_migrations.release.backup.subprocess.run", readiness)
    monkeypatch.setattr("src.infra.data_migrations.release.backup.time.sleep", sleeps.append)
    wait_until_ready("owned-restore", tmp_path)
    assert len(sleeps) == 2


class Target:
    """A durable environment model, including a deployable application boundary."""

    def __init__(self, failure=None, *, online=False):
        self.completed = set()
        self.events = []
        self.failure = failure
        self.accepted = False
        self.online = online
        self.mutex = Lock()
        self.held = False

    @contextmanager
    def lock(self):
        if not self.mutex.acquire(blocking=False):
            raise RuntimeError("busy")
        self.held = True
        try:
            yield self
        finally:
            self.held = False
            self.mutex.release()

    def assert_held(self):
        if not self.held:
            raise RuntimeError("lost lock")

    def event(self, name):
        self.assert_held()
        self.events.append(name)
        if self.failure == name:
            raise RuntimeError(name)

    def begin(self, source, plan):
        self.event("begin")
        return not self.accepted

    def prepare_build(self, source):
        self.event("build")

    def pending(self, phase):
        return phase.id not in self.completed

    def requires_quiescence(self, pending):
        return bool(pending) and not self.online

    def checkpoint(self, name, result=None):
        self.event("checkpoint:" + name)

    def quiesce(self):
        self.event("quiesce")
        return {"stopped": True}

    def backup(self):
        self.event("backup")
        return {"restorable": True}

    def schema(self, phase):
        self.event(phase.id)
        self.completed.add(phase.id)

    def data(self, phase):
        self.event(phase.id)
        self.completed.add(phase.id)

    def verify_database(self):
        self.event("verify")

    def deploy(self, source):
        self.event("deploy")

    def accept(self):
        self.event("accept")
        return {"read_write": True}

    def complete(self, source):
        self.event("complete")
        self.accepted = True

    def fail(self, phase, error_type):
        self.events.append("failed:" + phase)


def test_upgrade_orders_cutover_and_is_a_noop_after_acceptance():
    target = Target()
    assert Release(PLAN, target).run(SOURCE)["state"] == "accepted"
    operations = [e for e in target.events if not e.startswith("checkpoint:")]
    assert operations == [
        "begin",
        "build",
        "quiesce",
        "backup",
        "expand",
        "copy",
        "contract",
        "verify",
        "deploy",
        "accept",
        "complete",
    ]
    target.events.clear()
    assert Release(PLAN, target).run(SOURCE)["state"] == "already_accepted"
    assert target.events == ["begin"]


@pytest.mark.parametrize(
    "failure,forbidden",
    [
        ("build", "quiesce"),
        ("quiesce", "backup"),
        ("backup", "expand"),
        ("expand", "copy"),
        ("copy", "contract"),
        ("contract", "deploy"),
        ("verify", "deploy"),
        ("deploy", "accept"),
        ("accept", "complete"),
    ],
)
def test_failure_never_advances_or_marks_acceptance(failure, forbidden):
    target = Target(failure)
    with pytest.raises(RuntimeError, match=failure):
        Release(PLAN, target).run(SOURCE)
    assert forbidden not in target.events
    assert not target.accepted
    assert not target.held


def test_interruption_after_data_commit_resumes_without_repeating_copy():
    target = Target("checkpoint:copy")
    with pytest.raises(RuntimeError):
        Release(PLAN, target).run(SOURCE)
    assert target.completed == {"expand", "copy"}
    target.failure = None
    Release(PLAN, target).run(SOURCE)
    assert target.events.count("copy") == 1
    assert target.events.count("contract") == 1
    assert target.accepted


def test_acceptance_retry_does_not_quiesce_or_migrate_again():
    target = Target("accept")
    with pytest.raises(RuntimeError):
        Release(PLAN, target).run(SOURCE)
    target.failure = None
    Release(PLAN, target).run(SOURCE)
    assert target.events.count("backup") == 1
    assert target.events.count("copy") == 1
    assert target.events.count("accept") == 2


def test_additive_upgrade_keeps_existing_application_online():
    target = Target(online=True)
    Release(ReleasePlan((PLAN.phases[0],), "sum", frozenset({"1"})), target).run(SOURCE)
    assert "quiesce" not in target.events and "backup" not in target.events
    assert target.accepted


def test_environment_lock_covers_acceptance_and_rejects_second_release():
    target = Target()

    def accept():
        with pytest.raises(RuntimeError, match="busy"):
            Release(PLAN, target).run("b" * 40)
        return {"verified": True}

    target.accept = accept
    Release(PLAN, target).run(SOURCE)
    assert target.accepted


def test_lost_database_lock_blocks_application_deployment():
    target = Target()
    target.verify_database = lambda: setattr(target, "held", False)
    with pytest.raises(RuntimeError, match="lost lock"):
        Release(PLAN, target).run(SOURCE)
    assert "deploy" not in target.events
    assert not any(event.startswith("failed:") for event in target.events)
    assert not target.accepted


def test_checked_in_plan_covers_every_schema_and_data_dependency():
    plan = ReleasePlan.load(ROOT)
    assert plan.phases[-1].value == "latest"
    assert len(plan.schema_versions) == len(list((ROOT / "supabase/migrations").glob("*.sql")))
    assert {p.value for p in plan.phases if p.kind == "data"} >= {
        "20260927_entrypoint_storage_backfill",
        "20261003_final_entrypoint_storage",
        "20261007_native_repository_inventory",
        "20261007_repository_recovery_archive",
    }


def test_completion_receipt_does_not_call_storage_on_ordinary_release():
    target = object.__new__(DatabaseTarget)
    target.catalog = SimpleNamespace(get=lambda _: SimpleNamespace(checksum="sum"))
    target.schema_executor = SimpleNamespace(applied=lambda: {"contract"})
    target.db = SimpleNamespace(
        applied_schema_versions=lambda: {"contract"},
        receipt=lambda _: {"artifact_checksum": "sum", "verified": True},
    )
    assert not target.pending(Phase("data", "data", "artifact"))
    target.db.receipt = lambda _: {"artifact_checksum": "tampered", "verified": True}
    with pytest.raises(ValueError, match="differs"):
        target.pending(Phase("data", "data", "artifact"))


def test_retired_data_verifier_is_not_executed_after_its_tables_were_removed():
    target = object.__new__(DatabaseTarget)
    target.schema_executor = SimpleNamespace(applied=lambda: {"contract"})
    target.db = SimpleNamespace(applied_schema_versions=lambda: {"contract"})
    assert not target.pending(Phase("data", "data", "artifact", until_schema="contract"))


def test_hosted_drain_keeps_all_consumers_alive_until_queue_is_empty(monkeypatch):
    target = object.__new__(Hosted)
    stopped = []
    target.config = {"queue_names": ["sync"], "environment": "staging"}
    target.railway = SimpleNamespace(
        stop=lambda roles: stopped.append(set(roles)) or {"stopped": roles}
    )
    checks = iter([1, 0])

    def zcard(name):
        assert "synchronize_worker" not in set().union(*stopped)
        return next(checks)

    target.redis = SimpleNamespace(zcard=zcard)
    target.db = SimpleNamespace(scalar=lambda sql: "t" if "project_write_leases" in sql else "f")
    monkeypatch.setattr("src.infra.data_migrations.release.hosted.time.sleep", lambda _: None)
    result = target.quiesce()
    assert stopped == [{"api", "frontend", "mcp_server"}, ROLES - {"api", "frontend", "mcp_server"}]
    assert result["old_consumers_exited"] is True


def railway(handler, *, clock=lambda: 0):
    return Railway(
        {"project_id": "p", "environment_id": "e", "services": {"api": {"id": "s"}}},
        "private",
        client=httpx.Client(
            base_url="https://provider.test", transport=httpx.MockTransport(handler)
        ),
        clock=clock,
        sleep=lambda _: None,
    )


def test_railway_never_deploys_latest_branch_instead_of_candidate():
    import json

    calls = []

    def handler(request):
        call = json.loads(request.content)
        calls.append(call)
        if "serviceInstanceDeployV2" in call["query"]:
            assert call["variables"]["sha"] == SOURCE
            assert "commitSha:$sha" in call["query"]
            return httpx.Response(200, json={"data": {"serviceInstanceDeployV2": "deployment"}})
        if "deployment(id:" in call["query"]:
            assert call["variables"] == {"id": "deployment"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "deployment": {
                            "id": "deployment",
                            "status": "SUCCESS",
                            "meta": {"commitHash": SOURCE},
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "serviceInstance": {
                        "latestDeployment": {
                            "id": "deployment",
                            "status": "SUCCESS",
                            "meta": {"commitHash": "b" * 40},
                        }
                    }
                }
            },
        )

    assert railway(handler).deploy(SOURCE) == {"api": "deployment"}
    assert len(calls) == 3


def test_railway_independent_autodeploy_is_rejected_before_cutover():
    client = railway(
        lambda _: httpx.Response(
            200,
            json={
                "data": {
                    "serviceInstanceAutoDeployStatus": {"enabled": True},
                    "serviceInstance": {"latestDeployment": None},
                }
            },
        )
    )
    with pytest.raises(ValueError, match="autodeploy"):
        client.preflight()


@pytest.mark.parametrize("repository", [None, "unrelated/repository", "puppyone-ai/puppyone-cloud"])
def test_manual_deployment_requires_disabled_autodeploy_and_attached_repository(repository):
    client = railway(
        lambda _: httpx.Response(
            200,
            json={
                "data": {
                    "serviceInstanceAutoDeployStatus": {"enabled": False},
                    "serviceInstance": {"source": {"repo": repository}, "latestDeployment": None},
                }
            },
        )
    )
    if repository == "puppyone-ai/puppyone-cloud":
        assert client.preflight() == {"api": None}
    else:
        with pytest.raises(ValueError, match="repository"):
            client.preflight()


@pytest.mark.parametrize("public_acl,public_policy", [(True, False), (False, True), (False, None)])
def test_backup_bucket_must_be_proven_private(public_acl, public_policy):
    target = object.__new__(Hosted)
    target.config = {"backup_bucket": "private-backup"}
    grants = (
        [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AllUsers"}}]
        if public_acl
        else []
    )
    target.s3 = SimpleNamespace(
        get_bucket_acl=lambda **_: {"Grants": grants},
        get_bucket_policy_status=lambda **_: {"PolicyStatus": {"IsPublic": public_policy}},
    )
    with pytest.raises(ValueError):
        target.verify_private_backup_bucket()


@pytest.mark.parametrize("agent_state", ["succeeded", "failed", "wrong_reply", "approval"])
def test_acceptance_requires_stored_file_and_real_reply_and_always_cleans_up(
    monkeypatch, agent_state
):
    import json

    from src.infra.data_migrations.release.acceptance import accept

    calls, saved = [], {}

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        body = json.loads(request.content) if request.content else None
        result = {}
        if path.endswith("/auth/login"):
            result = {"access_token": "synthetic"}
        elif request.method == "POST" and path.endswith("/projects/"):
            result = {"id": "owned"}
        elif path.endswith("/write"):
            saved["content"] = body["content"]
            result = {"commit_id": "git-commit"}
        elif path.endswith("/cat"):
            result = {"content_text": saved["content"]}
        elif request.method == "POST" and path.endswith("/agents/runs"):
            result = {"id": "run"}
        elif request.method == "GET" and path.endswith("/agents/runs/run"):
            result = {
                "state": "succeeded"
                if agent_state in {"succeeded", "wrong_reply"}
                else agent_state,
                "snapshot": {
                    "text": saved["content"] if agent_state != "wrong_reply" else "did not read it"
                },
                "tools": [{"call_id": "tool", "state": "waiting"}]
                if agent_state == "approval"
                else [],
            }
        elif "/approvals/" in path:
            assert body["allow"] is False
        return httpx.Response(200, json={"data": result})

    client = httpx.Client(
        base_url="https://acceptance.test", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(
        "src.infra.data_migrations.release.acceptance.httpx.Client", lambda **_: client
    )
    config = {
        "api_url": "https://acceptance.test",
        "email": "fixture@example.invalid",
        "password": "synthetic",
        "org_id": "owned",
    }
    if agent_state == "succeeded":
        assert accept(config)["agent_reply"] is True
        assert ("POST", "/api/v1/agents/runs/run/stop") not in calls
    else:
        with pytest.raises(RuntimeError):
            accept(config)
    assert calls[-1] == ("DELETE", "/api/v1/projects/owned")
