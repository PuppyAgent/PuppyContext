"""Bare phase acceptance through formal creation, billing and credential APIs."""

import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime

import pytest

from src.platform.entitlements.models import (
    REQUIRED_ENTITLEMENT_FEATURES,
    REQUIRED_ENTITLEMENT_LIMITS,
    EntitlementUpsert,
)
from tests.repository_hosting.harness.application import Application
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.workflows import WORKFLOWS
from tests.repository_hosting.integration.test_docker_application import authorize_git

pytestmark = pytest.mark.hosting_application


@pytest.fixture(scope="module")
def bare_application(tmp_path_factory):
    app = Application(
        tmp_path_factory.mktemp("bare-app"),
        profile="new bare repositories; formal creation/entitlement/credential APIs; Scope disabled",
    )
    try:
        app.start()
        yield app
    finally:
        app.close()


def publish_entitlement(app, org, *, max_file_bytes=8 * 1024**2, max_storage_bytes=64 * 1024**2):
    # Simulate the billing publisher at its real authenticated ingress; no SQL
    # entitlement/enrollment fixtures and no external billing network calls.
    limits = {key: 100 for key in REQUIRED_ENTITLEMENT_LIMITS}
    limits.update(
        {
            "storage.max_bytes": max_storage_bytes,
            "upload.max_single_file_bytes": max_file_bytes,
            "seats.purchased": 1,
        }
    )
    payload = EntitlementUpsert(
        org_id=org,
        schema_version="1.0",
        plan_id="test-bare",
        status="active",
        source="puppypay",
        entitlements={
            "features": {key: True for key in REQUIRED_ENTITLEMENT_FEATURES},
            "limits": limits,
            "allow": {"access_surface_kinds": "*"},
        },
        seat_quantity=1,
        catalog_version="test-1",
        source_revision=1,
        effective_at=datetime.now(UTC),
        payload_hash="0" * 64,
    ).model_dump(mode="json")
    unsigned = {key: value for key, value in payload.items() if key != "payload_hash"}
    if unsigned.get("source_quote_id") is None:
        unsigned.pop("source_quote_id", None)
    payload["payload_hash"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    response = app.client.post(
        "/internal/billing/entitlements/upsert",
        json=payload,
        headers={"X-Internal-Secret": app.env["INTERNAL_API_SECRET"]},
    )
    assert response.status_code == 200, response.text


def create_bare(app, *, format="sha1", branch="trunk"):
    org = app.api(
        "POST",
        "/organizations/",
        expected=201,
        json={"name": "Bare acceptance " + uuid.uuid4().hex},
    )["id"]
    publish_entitlement(app, org)
    key = str(uuid.uuid4())
    payload = {
        "name": "Bare repo",
        "org_id": org,
        "repository": {"profile": "native", "object_format": format, "default_branch": branch},
    }
    project = app.api(
        "POST", "/projects/", expected=201, headers={"Idempotency-Key": key}, json=payload
    )["id"]
    replay = app.api(
        "POST", "/projects/", expected=200, headers={"Idempotency-Key": key}, json=payload
    )
    assert replay["id"] == project
    secret = "pwg_" + secrets.token_urlsafe(32)
    issued = app.api(
        "POST",
        f"/projects/{project}/git-credentials",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "target": {"kind": "project_root", "project_id": project},
            "mode": "rw",
            "credential": secret,
        },
    )
    return org, project, issued["remote"]["url"], secret


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_bare_formal_creation_git_head_replay_and_cold_read(bare_application, tmp_path, format):
    app = bare_application
    _org, project, remote, secret = create_bare(app, format=format)
    pg = Postgres()
    assert (
        pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}")
        == "0"
    )
    assert (
        pg.value(
            f"SELECT coalesce(version_root_hash,'') FROM public.projects WHERE id={literal(project)}"
        )
        == ""
    )
    initial_head = app.api("GET", f"/content/{project}/head")
    assert initial_head["head_commit_id"] == ""
    assert initial_head["head"]["kind"] == "symbolic"
    readiness = app.api("GET", f"/projects/{project}/readiness")
    assert readiness["git"]["state"] == "awaiting_first_push"
    assert readiness["git"]["default_branch"] == "trunk"
    client = Git.init(tmp_path / "client", format=format)
    authorize_git(client, secret)
    client.run("branch", "-m", "trunk")
    client.run("remote", "add", "origin", remote)
    first = client.commit({"readme": b"bare\n"})
    client.run("push", "-u", "origin", "trunk")
    readiness = app.api("GET", f"/projects/{project}/readiness")
    assert readiness["git"]["state"] == "ready"
    assert readiness["claude"]["ready"] is True
    auth = (
        "http.extraHeader=Authorization: Basic "
        + base64.b64encode(("x:" + secret).encode()).decode()
    )
    client.run("-c", auth, "clone", remote, tmp_path / "before-head-change")
    assert Git(tmp_path / "before-head-change").text("symbolic-ref", "HEAD") == "refs/heads/trunk"
    client.run("branch", "feature")
    client.run("tag", "-a", "v1", "-m", "annotated")
    client.run("push", "--atomic", "origin", "feature", "refs/tags/v1")
    payload = {
        "request_key": str(uuid.uuid4()),
        "generation": 1,
        "expected": {
            "kind": "symbolic",
            "target_b64": base64.b64encode(b"refs/heads/trunk").decode(),
        },
        "target_branch": "feature",
    }
    result = app.api("PUT", f"/content/{project}/head", json=payload)
    assert result["status"] == "committed"
    current_head = app.api("GET", f"/content/{project}/head")
    assert current_head["head_commit_id"] == first
    assert current_head["head"]["target_b64"] == base64.b64encode(b"refs/heads/feature").decode()
    client.run("-c", auth, "clone", remote, tmp_path / "after-head-change")
    assert Git(tmp_path / "after-head-change").text("symbolic-ref", "HEAD") == "refs/heads/feature"
    assert app.api("PUT", f"/content/{project}/head", json=payload) == result
    for changed in ({"target_branch": "trunk"}, {"generation": 2}):
        app.request(
            "PUT",
            f"/api/v1/content/{project}/head",
            json={**payload, **changed},
            expected=409,
        )
    assert app.api("GET", f"/content/{project}/head") == current_head
    app.request(
        "PUT",
        f"/api/v1/content/{project}/head",
        json={**payload, "request_key": str(uuid.uuid4())},
        expected=409,
    )
    app.stop()
    app.start()
    cold = Git.init(tmp_path / "cold.git", bare=True, format=format)
    authorize_git(cold, secret)
    cold.run("fetch", remote, "+refs/*:refs/*")
    cold.run("fsck", "--full", "--strict")
    assert cold.text("rev-parse", "refs/heads/trunk") == first
    assert cold.run("show", first + ":readme").stdout == b"bare\n"
    assert b"ref: refs/heads/feature\tHEAD" in client.run("ls-remote", "--symref", "origin").stdout
    # Scope creation fails before any usable resource is persisted.
    denied = pg.sql(
        f"SET ROLE service_role; INSERT INTO public.repository_scopes(id,project_id,name,path) VALUES('native-scope',{literal(project)},'docs','docs')",
        check=False,
    )
    assert denied.returncode != 0
    assert "native_scope_not_supported" in denied.stderr
    assert (
        pg.value(
            f"SELECT count(*) FROM public.repository_scopes WHERE project_id={literal(project)}"
        )
        == "0"
    )


@pytest.mark.parametrize("format", ["sha1", "sha256"])
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda workflow: workflow.name)
def test_bare_canonical_git_workflow_matches_stock(
    bare_application, workflow, format, tmp_path, monkeypatch, request
):
    from tests.repository_hosting.harness import conformance, http, network_workflows

    app = bare_application
    _org, _project, remote, secret = create_bare(app, branch="main", format=format)
    # The same existing recipes use the actually issued credential on every
    # peer/clone. Only test-client configuration changes; server auth is real.
    auth = (
        "http.extraHeader=Authorization: Basic "
        + base64.b64encode(("x:" + secret).encode()).decode()
    )
    for module in (http, conformance, network_workflows):
        monkeypatch.setattr(module, "AUTH", auth)
    source = Git.init(tmp_path / "source", format=format)
    source.commit({"original.txt": b"original\n", "binary": bytes(range(256))}, "base")
    authorize_git(source, secret)
    oracle = Git.init(tmp_path / "oracle.git", bare=True, format=format)
    oracle.run("config", "uploadpack.allowFilter", "true")
    oracle.run("config", "uploadpack.allowAnySHA1InWant", "true")
    source.run("push", oracle.path.as_uri(), "main")
    expected_client = http.clone_client(oracle.path.as_uri(), tmp_path / "expected", source)
    workflow.execute(expected_client)
    expected = conformance.remote_snapshot(source, oracle.path.as_uri(), tmp_path / "expected.git")
    source.run("push", remote, "main")
    actual_client = http.clone_client(remote, tmp_path / "actual", source)
    workflow.execute(actual_client)
    actual = conformance.remote_snapshot(source, remote, tmp_path / "actual.git")
    assert actual == expected
    request.node.user_properties.extend(
        [
            ("git_workflow", workflow.name),
            ("git_profile", "formal-native-bare-" + format),
            (
                "storage_evidence",
                "formal creation + billing ingress + real credentials + canonical HTTP + PG/S3",
            ),
        ]
    )


def test_bare_formal_authorization_scope_denial_and_native_default(bare_application, tmp_path):
    app, pg = bare_application, Postgres()
    org, project, remote, secret = create_bare(app, branch="main")
    source = Git.init(tmp_path / "source")
    authorize_git(source, secret)
    accepted = source.commit({"readme": b"authorized"})
    source.run("push", remote, "main")
    neighbor = app.api(
        "POST",
        "/projects/",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"name": "Default native neighbor", "org_id": org},
    )["id"]
    assert (
        pg.value(
            f"SELECT count(*) FROM public.version_repositories WHERE project_id={literal(neighbor)} AND authority='native'"
        )
        == "1"
    )
    assert (
        pg.value(
            f"SELECT coalesce(version_root_hash,'') FROM public.projects WHERE id={literal(neighbor)}"
        )
        == ""
    )
    denied = app.request(
        "POST",
        f"/api/v1/projects/{project}/scopes",
        expected=409,
        json={"name": "docs", "path": "docs"},
    )
    assert "native_scope_not_supported" in denied.text
    assert (
        pg.value(
            f"SELECT count(*) FROM public.repository_scopes WHERE project_id={literal(project)}"
        )
        == "0"
    )
    reader_secret = "pwg_" + secrets.token_urlsafe(32)
    reader = app.api(
        "POST",
        f"/projects/{project}/git-credentials",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "target": {"kind": "project_root", "project_id": project},
            "mode": "r",
            "credential": reader_secret,
        },
    )
    auth = {"Authorization": "Basic " + base64.b64encode(("x:" + reader_secret).encode()).decode()}
    advertised = f"/git/{project}.git/info/refs"
    assert (
        app.client.get(advertised, headers=auth, params={"service": "git-upload-pack"}).status_code
        == 200
    )
    assert (
        app.client.get(advertised, headers=auth, params={"service": "git-receive-pack"}).status_code
        == 403
    )
    assert (
        app.client.get(
            f"/git/{neighbor}.git/info/refs", headers=auth, params={"service": "git-upload-pack"}
        ).status_code
        == 401
    )
    assert app.client.get(
        f"/git/{project}/scopes/unknown.git/info/refs",
        headers=auth,
        params={"service": "git-upload-pack"},
    ).status_code in (401, 403, 404)
    app.api("DELETE", f"/projects/{project}/git-credentials/{reader['id']}")
    assert (
        app.client.get(advertised, headers=auth, params={"service": "git-upload-pack"}).status_code
        == 401
    )
    pg.sql(
        f"UPDATE public.version_repositories SET write_state='fenced' WHERE project_id={literal(project)}"
    )
    source.commit({"readme": b"must not publish"})
    assert source.run("push", remote, "main", check=False).returncode != 0
    assert (
        pg.value(
            f"SELECT target_oid FROM public.version_repository_refs WHERE project_id={literal(project)} AND name=convert_to('refs/heads/main','UTF8')"
        )
        == accepted
    )
