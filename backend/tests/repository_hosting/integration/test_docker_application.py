"""Real JWT/Git credential -> src.main -> native Product writes -> PG/S3.

No dependency overrides. This is not native routing/quota or hosted acceptance.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import uuid

import pytest

from tests.repository_hosting.harness.application import Application
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.supabase_api import SupabaseAPI

pytestmark = pytest.mark.hosting_application


@pytest.fixture
def application(tmp_path):
    app = Application(tmp_path)
    try:
        app.start()
        yield app
    finally:
        app.close()


def authorize_git(repo, credential):
    header = (
        "Authorization: Basic "
        + base64.b64encode(("x-puppyone-token:" + credential).encode()).decode()
    )
    result = repo.run("config", "--local", "http.extraHeader", header, check=False)
    if result.returncode:
        raise AssertionError("failed to install private test Git authentication")
    path = repo.path / (".git/config" if (repo.path / ".git").is_dir() else "config")
    path.chmod(0o600)


def test_docker_application_real_auth_git_api_cold_restart_and_revocation(application, tmp_path):
    from tests.repository_hosting.integration.test_bare_repository_application import (
        publish_entitlement,
    )

    app = application
    org = app.api("POST", "/organizations/", expected=201, json={"name": "Docker hosting"})
    publish_entitlement(app, org["id"])
    project = app.api(
        "POST",
        "/projects/",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"name": "Authenticated repository", "org_id": org["id"]},
    )["id"]
    credential = "pwg_" + secrets.token_urlsafe(32)
    issued = app.api(
        "POST",
        f"/projects/{project}/git-credentials",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "target": {"kind": "project_root", "project_id": project},
            "mode": "rw",
            "credential": credential,
        },
    )
    remote = issued["remote"]["url"]
    assert remote == app.url + f"/git/{project}.git" and credential not in remote
    client = Git.init(tmp_path / "client")
    authorize_git(client, credential)
    client.run("fetch", remote, "+refs/heads/*:refs/heads/*")
    if client.run("show-ref", "--verify", "refs/heads/main", check=False).returncode == 0:
        client.run("reset", "--hard", "main")
    first = client.commit({"from-git.md": b"Git bytes\n"})
    client.run("push", remote, "main")
    path = f"/api/v1/content/{project}/raw"
    assert app.request("GET", path, params={"path": "from-git.md"}).content == b"Git bytes\n"
    written = app.api(
        "POST",
        f"/content/{project}/write",
        json={
            "path": "from-api.md",
            "content": "API bytes\n",
            "node_type": "markdown",
            "base_commit_id": first,
        },
    )
    api_commit = written["commit_id"]
    assert api_commit != first
    bulk = app.api(
        "POST",
        f"/content/{project}/bulk-write",
        json={
            "files": [{"path": "from-bulk.md", "content": "Bulk bytes\n", "node_type": "markdown"}],
            "base_commit_id": api_commit,
        },
    )
    commit = bulk["commit_id"]
    assert commit not in (first, api_commit)
    for stale in (first, api_commit, ""):
        app.request(
            "POST",
            f"/api/v1/content/{project}/bulk-write",
            expected=409,
            json={
                "files": [
                    {
                        "path": "from-api.md",
                        "content": "stale bulk overwrite",
                        "node_type": "markdown",
                    }
                ],
                "base_commit_id": stale,
            },
        )
    app.request(
        "POST",
        f"/api/v1/content/{project}/bulk-write",
        token=False,
        expected=401,
        json={
            "files": [{"path": "from-api.md", "content": "anonymous", "node_type": "markdown"}],
            "base_commit_id": commit,
        },
    )
    app.request(
        "POST",
        f"/api/v1/content/{project}/write",
        expected=409,
        json={
            "path": "from-api.md",
            "content": "stale overwrite",
            "node_type": "markdown",
            "base_commit_id": first,
        },
    )
    app.request("GET", path, token=False, expected=401, params={"path": "from-api.md"})
    stranger = SupabaseAPI(os.environ)
    try:
        stranger.authenticate()
        denied = app.client.get(
            path,
            params={"path": "from-api.md"},
            headers={"Authorization": stranger.headers("authenticated")["Authorization"]},
        )
        assert denied.status_code in (403, 404)
    finally:
        stranger.close()
    app.stop()
    cache = app.directory / "git-cache"
    if cache.exists():
        shutil.rmtree(cache)
    app.start()
    assert len(set(app.starts)) == 2
    assert app.request("GET", path, params={"path": "from-api.md"}).content == b"API bytes\n"
    assert app.request("GET", path, params={"path": "from-bulk.md"}).content == b"Bulk bytes\n"
    cold = Git.init(tmp_path / "cold.git", bare=True)
    authorize_git(cold, credential)
    cold.run("fetch", remote, "+refs/heads/*:refs/heads/*")
    assert cold.text("rev-parse", "refs/heads/main") == commit
    assert cold.run("show", f"{commit}:from-git.md").stdout == b"Git bytes\n"
    assert cold.run("show", f"{commit}:from-api.md").stdout == b"API bytes\n"
    assert cold.run("show", f"{commit}:from-bulk.md").stdout == b"Bulk bytes\n"
    assert cold.run("show", f"{api_commit}:from-api.md").stdout == b"API bytes\n"
    assert cold.run("show", f"{first}:from-git.md").stdout == b"Git bytes\n"
    cold.run("fsck", "--full", "--strict")
    app.api("DELETE", f"/projects/{project}/git-credentials/{issued['id']}")
    assert cold.run("ls-remote", remote, check=False).returncode != 0
    # Explicit credential revocation does not erase earlier acknowledged data.
    assert app.request("GET", path, params={"path": "from-api.md"}).content == b"API bytes\n"
