"""Actual HTTP/service/policy; repository, OAuth and GitHub I/O are isolated facts."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.synchronize.github import public_router as public
from src.platform.synchronize.github import router as legacy
from src.platform.synchronize.github import service as services
from src.platform.synchronize.github import webhook as hooks
from src.platform.synchronize.github.schemas import GithubSyncRunResult
from tests.authorization_fakes import authorization_for, install_authorization

BASE = "/api/v1/projects/project-1/synchronize/github"
OLD = "/api/v1/projects/project-1/github"


class Memory:
    def __init__(self):
        self.rows = {}
        self.logs = []
        self.writes = []

    async def get_by_project(self, project_id):
        return self.rows.get(project_id)

    async def upsert(self, project_id, fields):
        now = datetime.now(UTC)
        row = {
            "id": f"github-binding-{len(self.writes) + 1}",
            "project_id": project_id,
            "created_at": now,
            "updated_at": now,
            **self.rows.get(project_id, {}),
            **fields,
        }
        self.rows[project_id] = row
        self.writes.append(dict(row))
        return row

    async def delete_by_project(self, project_id):
        return self.rows.pop(project_id, None) is not None

    async def find_by_repo(self, owner, name):
        return [
            row
            for row in self.rows.values()
            if (row["github_repo_owner"], row["github_repo_name"]) == (owner, name)
        ]

    async def list_recent(self, binding_id, *, limit, offset):
        rows = [row for row in self.logs if row["synchronize_github_binding_id"] == binding_id]
        return rows[offset : offset + limit], len(rows)


@pytest.fixture
def environment(monkeypatch):
    memory = Memory()
    monkeypatch.setattr(services, "GithubSyncRepository", lambda: memory)
    monkeypatch.setattr(services, "GithubSyncLogRepository", lambda: memory)
    monkeypatch.setattr(hooks, "GithubSyncRepository", lambda: memory)
    oauth = AsyncMock(return_value=SimpleNamespace(user_id="user-1", provider="github"))
    monkeypatch.setattr(legacy, "OAuthRepository", lambda: SimpleNamespace(get_by_id=oauth))
    executions = []

    async def run(binding, direction, **options):
        executions.append((binding["id"], direction, options))
        result = GithubSyncRunResult(
            status="success",
            direction=direction,
            git_sha="sha",
            version_commit_id="commit",
            files_changed=1,
        )
        memory.logs.append(
            {
                "id": f"log-{len(executions)}",
                "synchronize_github_binding_id": binding["id"],
                "created_at": datetime.now(UTC),
                **result.model_dump(),
            }
        )
        return result

    async def pull(binding, **options):
        return await run(binding, "inbound", **options)

    async def push(binding, **options):
        return await run(binding, "outbound", **options)

    monkeypatch.setattr(services, "import_branch", pull)
    monkeypatch.setattr(services, "export_to_branch", push)
    app = FastAPI()
    for router in (public.router, public.webhook_router, legacy.router, legacy.webhook_router):
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="user-1", role="authenticated"
    )
    install_authorization(app, authorization_for("project-1"))
    return app, memory, oauth, executions


def create_body():
    return {
        "oauth_connection_id": 1,
        "github_repo_owner": "owner",
        "github_repo_name": "repo",
        "auto_pull": True,
        "webhook_secret": "test-only-secret",
    }


def test_canonical_binding_lifecycle_execution_identity_and_logs(environment):
    app, memory, _, executions = environment
    with TestClient(app) as client:
        assert client.get(BASE + "/binding").json()["data"] is None
        result = client.post(BASE + "/binding", json=create_body())
        assert result.status_code == 200, result.text
        row = result.json()["data"]
        assert row["auto_pull"] and row["has_webhook_secret"]
        assert row["last_pulled_sha"] is None and row["last_pushed_at"] is None
        assert "test-only-secret" not in result.text and "auto_import" not in row
        assert client.get(BASE + "/binding").json()["data"]["id"] == row["id"]
        assert (
            client.patch(BASE + "/binding", json={"default_branch": "feature"}).json()["data"]["id"]
            == row["id"]
        )
        for operation, direction in (("pull", "inbound"), ("push", "outbound")):
            executed = client.post(BASE + f"/{operation}", json={})
            assert executed.status_code == 200, executed.text
            assert executed.json()["data"]["synchronize_github_binding_id"] == row["id"]
            assert executed.json()["data"]["direction"] == direction
        logs = client.get(BASE + "/logs?limit=1&offset=1").json()["data"]
        assert logs["total"] == 2 and len(logs["entries"]) == 1
        assert (
            logs["synchronize_github_binding_id"]
            == logs["entries"][0]["synchronize_github_binding_id"]
            == row["id"]
        )
        assert logs["entries"][0]["direction"] == "outbound"
        assert "integration_id" not in json.dumps(logs)
        assert client.delete(BASE + "/binding").status_code == 200
        missing = client.post(BASE + "/pull", json={})
        assert (
            missing.status_code == 404
            and missing.json()["detail"]["code"] == "SYNCHRONIZE_GITHUB_BINDING_NOT_FOUND"
        )
    assert len(executions) == 2 and memory.rows == {}


@pytest.mark.parametrize("base", [BASE, OLD])
@pytest.mark.parametrize("role", ["viewer", "foreign"])
def test_real_project_policy_blocks_before_oauth_persistence_or_execution(environment, base, role):
    app, memory, oauth, executions = environment
    install_authorization(
        app, authorization_for("project-1", role=role) if role != "foreign" else authorization_for()
    )
    canonical = base == BASE
    body = create_body()
    if not canonical:
        body["auto_import"] = body.pop("auto_pull")
    with TestClient(app) as client:
        for method, suffix, payload in (
            ("post", "/binding" if canonical else "/connect", body),
            ("patch", "/binding" if canonical else "", {}),
            ("delete", "/binding" if canonical else "", None),
            ("post", "/pull" if canonical else "/import", {}),
            ("post", "/push" if canonical else "/export", {}),
        ):
            response = client.request(
                method, base + suffix, **({"json": payload} if payload is not None else {})
            )
            assert response.status_code in (403, 404), response.text
        if role == "foreign":
            for suffix in (
                "/binding" if canonical else "/status",
                "/repos?oauth_connection_id=1",
                "/branches?oauth_connection_id=1&repo_owner=o&repo_name=n",
                "/logs" if canonical else "/sync-log",
            ):
                assert client.get(base + suffix).status_code in (403, 404)
    oauth.assert_not_called()
    assert memory.writes == [] and executions == []


@pytest.mark.parametrize("base", [BASE, OLD])
@pytest.mark.parametrize(
    "oauth_row",
    [
        None,
        SimpleNamespace(user_id="another", provider="github"),
        SimpleNamespace(user_id="user-1", provider="gmail"),
    ],
)
def test_oauth_ids_do_not_grant_another_account_or_provider(environment, base, oauth_row):
    app, memory, oauth, executions = environment
    oauth.return_value = oauth_row
    body = create_body()
    if base == OLD:
        body["auto_import"] = body.pop("auto_pull")
    with TestClient(app) as client:
        assert (
            client.post(base + ("/binding" if base == BASE else "/connect"), json=body).status_code
            == 404
        )
        assert client.get(base + "/repos?oauth_connection_id=1").status_code == 404
        assert (
            client.get(
                base + "/branches?oauth_connection_id=1&repo_owner=o&repo_name=n"
            ).status_code
            == 404
        )
    assert memory.writes == [] and executions == []


@pytest.mark.parametrize(
    "query", ["integration_id=x", "connection_id=x", "limit=", "limit=1&limit=2", "unknown=x"]
)
def test_invalid_log_selectors_are_not_ignored(environment, query):
    app, _, _, _ = environment
    with TestClient(app) as client:
        assert client.get(BASE + "/logs?" + query).status_code == 422


def test_obsolete_and_ambiguous_body_fields_do_not_mutate(environment):
    app, memory, _, _ = environment
    with TestClient(app) as client:
        for extra in (
            {"auto_import": True},
            {"synchronize_binding_id": "other-domain"},
            {"default_branch": " "},
        ):
            assert (
                client.post(BASE + "/binding", json={**create_body(), **extra}).status_code == 422
            )
        assert client.patch(BASE + "/binding", json={"default_branch": None}).status_code == 422
        invalid = client.post(BASE + "/binding", json={**create_body(), "webhook_secret": None})
        assert invalid.status_code == 400 and "auto_pull" in invalid.text
    assert memory.writes == []


def test_canonical_webhook_keeps_raw_hmac_and_deduplication(environment, monkeypatch):
    app, _, _, _ = environment
    queue = AsyncMock(side_effect=["job-1", None])
    monkeypatch.setattr(
        "src.platform.synchronize.github.arq_client.get_github_sync_arq_client",
        lambda: SimpleNamespace(enqueue_pull=queue),
    )
    body = json.dumps(
        {
            "repository": {"owner": {"login": "owner"}, "name": "repo"},
            "ref": "refs/heads/main",
            "after": "new-sha",
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"test-only-secret", body, hashlib.sha256).hexdigest()
    headers = {
        "x-github-event": "push",
        "x-github-delivery": "delivery-1",
        "x-hub-signature-256": signature,
    }
    with TestClient(app) as client:
        row = client.post(BASE + "/binding", json=create_body()).json()["data"]
        url = "/api/v1/synchronize/github/webhook"
        first = client.post(url, content=body, headers=headers)
        second = client.post(url, content=body, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["results"][0]["synchronize_github_binding_id"] == row["id"]
        assert second.json()["results"][0]["reason"] == "already_queued"
        assert "integration_id" not in first.text and "test-only-secret" not in first.text
        assert client.post(url, content=body + b" ", headers=headers).status_code == 401
    assert queue.await_count == 2
    assert queue.await_args_list[0] == queue.await_args_list[1]
    assert queue.await_args.kwargs["dedup_key"] == f"gh-import:{row['id']}:new-sha"
