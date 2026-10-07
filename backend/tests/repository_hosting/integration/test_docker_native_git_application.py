"""Real credential -> canonical router -> checked native PG/S3 publication.

Projects use formal native creation and the real entitlement ingress with a
local test publisher. This does not claim live external PuppyPay acceptance.
Selected Product read APIs use real JWT admission and the same native refs.
"""

import base64
import json
import secrets
import uuid

import pytest

from tests.repository_hosting.harness.application import Application
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.integration.test_docker_application import authorize_git

pytestmark = pytest.mark.hosting_application


@pytest.fixture
def application(tmp_path):
    app = Application(
        tmp_path,
        profile="native Git and selected Product reads; formal creation and local billing publisher; Scope not accepted",
    )
    try:
        app.start()
        yield app
    finally:
        app.close()


def create_native_project(app, org, object_format):
    from tests.repository_hosting.integration.test_bare_repository_application import (
        publish_entitlement,
    )

    publish_entitlement(app, org, max_file_bytes=64, max_storage_bytes=4096)
    return app.api(
        "POST",
        "/projects/",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "name": "Native repository",
            "org_id": org,
            "repository": {
                "profile": "native",
                "object_format": object_format,
                "default_branch": "trunk",
            },
        },
    )["id"]


def assert_native_product_reads(app, pg, project, commit, format):
    legacy = pg.value(f"SELECT version_root_hash FROM public.projects WHERE id={literal(project)}")
    sequence = pg.value(
        f"SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(project)}"
    )
    for action in ("ls", "tree"):
        listing = app.api("GET", f"/content/{project}/{action}")
        assert [e["name"] for e in listing["entries"]] == ["readme"]
        assert listing["entries"][0]["git_mode"] == "100644"
        assert listing["head_commit_id"] == commit
        assert listing["repository_revision"]["target_ref"] == "refs/heads/trunk"
        assert listing["repository_revision"]["expected_oid"] == commit
        assert listing["repository_revision"]["object_format"] == format
    cat = app.api("GET", f"/content/{project}/cat", params={"path": "readme"})
    assert cat["content_text"] == "hello\n" and cat["head_commit_id"] == commit
    stat = app.api("GET", f"/content/{project}/stat", params={"path": "readme"})
    assert stat["exists"] and stat["head_commit_id"] == commit and stat["git_mode"] == "100644"
    raw = app.request(
        "GET",
        f"/api/v1/content/{project}/raw",
        params={"path_bytes_b64": base64.b64encode(b"readme").decode()},
    )
    assert raw.content == b"hello\n"
    assert json.loads(raw.headers["x-puppyone-repository-revision"])["expected_oid"] == commit
    assert raw.headers["cache-control"] == "private, no-store"
    for ref in (b"refs/heads/trunk", b"refs/tags/annotated"):
        selected = base64.b64encode(ref).decode()
        for action in ("ls", "tree", "cat", "stat", "raw"):
            params = {
                "ref_b64": selected,
                "path": "readme" if action in ("cat", "stat", "raw") else "",
            }
            response = app.request("GET", f"/api/v1/content/{project}/{action}", params=params)
            revision = (
                json.loads(response.headers["x-puppyone-repository-revision"])
                if action == "raw"
                else response.json()["data"]["repository_revision"]
            )
            assert revision["target_ref_b64"] == selected and revision["head_guard"] is None
    app.request(
        "GET",
        f"/api/v1/content/{project}/cat",
        params={"path": "", "ref_b64": base64.b64encode(b"refs/tags/blob").decode()},
        expected=400,
    )
    app.request(
        "GET",
        f"/api/v1/content/{project}/ls",
        params={"ref_b64": base64.b64encode(b"refs/heads/missing").decode()},
        expected=404,
    )
    app.request("GET", f"/api/v1/content/{project}/ls", params={"ref_b64": "bad!"}, expected=400)
    app.request("GET", f"/api/v1/content/{project}/ls", token=False, expected=401)
    assert (
        pg.value(f"SELECT version_root_hash FROM public.projects WHERE id={literal(project)}")
        == legacy
    )
    assert (
        pg.value(
            f"SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(project)}"
        )
        == sequence
    )
    assert (
        pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}")
        == "0"
    )


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_docker_native_git_canonical_auth_refs_policy_cold_restart(application, tmp_path, format):
    app, pg = application, Postgres()
    org = app.api("POST", "/organizations/", expected=201, json={"name": "Native Git routing"})[
        "id"
    ]
    project = create_native_project(app, org, format)
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
    headers = {"Authorization": "Basic " + base64.b64encode(("x:" + credential).encode()).decode()}
    health_path = f"/git/{project}.git/health"
    health = app.client.get(health_path, headers=headers)
    assert health.status_code == 200, health.text
    assert health.json()["data"]["health"] == "empty"
    assert health.json()["data"]["object_format"] == format
    client = Git.init(tmp_path / "native-client", format=format)
    authorize_git(client, credential)
    client.run("branch", "-m", "trunk")
    client.run("remote", "add", "origin", remote)
    first = client.commit({"readme": b"hello\n"})
    client.run("push", "-u", "origin", "trunk")
    # All-ref batches and typed tags traverse the public route, not the internal
    # ASGI native fixture. Default HEAD is metadata, not hard-coded main.
    client.run("branch", "topic")
    client.run("tag", "-a", "annotated", "-m", "retained tag")
    blob = client.text("rev-parse", first + ":readme")
    client.run("tag", "blob", blob)
    client.run("push", "--atomic", "origin", "topic", "refs/tags/annotated", "refs/tags/blob")
    before = client.run("ls-remote", "--symref", "origin").stdout
    assert b"ref: refs/heads/trunk\tHEAD" in before and b"refs/heads/main" not in before
    assert_native_product_reads(app, pg, project, first, format)
    rejected = client.commit({"too-large": b"x" * 65})
    oversized = client.text("rev-parse", rejected + ":too-large")
    assert client.run("push", "origin", "trunk", check=False).returncode != 0
    assert client.run("ls-remote", "--symref", "origin").stdout == before
    client.run("reset", "--hard", first)
    assert (
        pg.value(
            f"SELECT count(*) FROM public.version_repository_object_capacity WHERE project_id={literal(project)} AND object_id={literal(oversized)}"
        )
        == "0"
    )
    app.stop()
    app.start()
    cold = Git.init(tmp_path / "native-cold.git", bare=True, format=format)
    authorize_git(cold, credential)
    cold.run("-c", "protocol.version=2", "fetch", remote, "+refs/*:refs/*")
    assert cold.text("rev-parse", "refs/heads/trunk") == first
    assert cold.refs() == {
        "refs/heads/trunk": first,
        "refs/heads/topic": first,
        "refs/tags/annotated": client.text("rev-parse", "annotated"),
        "refs/tags/blob": blob,
    }
    assert cold.run("show", first + ":readme").stdout == b"hello\n"
    cold.run("fsck", "--full", "--strict")
    assert len(set(app.starts)) == 2
    assert_native_product_reads(app, pg, project, first, format)
    assert app.client.get(health_path, headers=headers).json()["data"]["health"] == "healthy"
    # A newly issued read credential can discover/fetch but cannot advertise a
    # receive service or mutate. Foreign locators do not retarget this grant.
    read_credential = "pwg_" + secrets.token_urlsafe(32)
    app.api(
        "POST",
        f"/projects/{project}/git-credentials",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "target": {"kind": "project_root", "project_id": project},
            "mode": "r",
            "credential": read_credential,
        },
    )
    read_headers = {
        "Authorization": "Basic " + base64.b64encode(("x:" + read_credential).encode()).decode()
    }
    assert (
        app.client.get(
            f"/git/{project}.git/info/refs",
            params={"service": "git-upload-pack"},
            headers=read_headers,
        ).status_code
        == 200
    )
    assert (
        app.client.get(
            f"/git/{project}.git/info/refs",
            params={"service": "git-receive-pack"},
            headers=read_headers,
        ).status_code
        == 403
    )
    assert (
        app.client.get(
            "/git/foreign.git/info/refs", params={"service": "git-upload-pack"}, headers=headers
        ).status_code
        == 401
    )
    assert (
        app.client.get(
            f"/git/{project}.git/info/refs", params={"service": "git-upload-pack"}
        ).status_code
        == 401
    )
    assert (
        pg.value(
            f"SELECT value FROM public.organization_usage_counters WHERE org_id={literal(org)} AND metric='storage.logical_bytes'"
        )
        == "6"
    )
    # The supervisor discovers this stock-Git-generated key from owned SQL.
    # This proves authenticated lookup of a known key, not Git wire negotiation
    # or recovery of an unknown key after a client loses the entire response.
    actor = "runtime:" + issued["id"]
    recorded = json.loads(
        pg.value(
            "SELECT to_jsonb(t) FROM public.version_ref_transactions t "
            f"WHERE project_id={literal(project)} AND actor={literal(actor)} ORDER BY created_at,id LIMIT 1"
        )
    )
    status_path = f"/git/{project}.git/operations/{recorded['request_key']}"
    pg.sql(
        f"UPDATE public.access_surface_credentials SET grant_mode='r' WHERE id={literal(issued['id'])};"
        f"UPDATE public.version_repository_file_policies SET initialized=false WHERE project_id={literal(project)};"
        f"DELETE FROM public.organization_entitlements WHERE org_id={literal(org)}"
    )

    def inventory():
        return [
            pg.value(f"SELECT count(*) FROM public.{table} WHERE project_id={literal(project)}")
            for table in (
                "project_write_leases",
                "version_object_pins",
                "version_ref_transactions",
                "version_ref_events",
                "version_repository_capacity_inflight",
                "version_product_operations",
            )
        ]

    before_lookup = inventory()
    metadata_path = f"/git/{project}.git/refs"
    metadata = app.client.get(metadata_path, headers=headers)
    assert metadata.status_code == 200 and metadata.headers["cache-control"] == "no-store"
    refs = metadata.json()["data"]
    human_refs = app.api("GET", f"/content/{project}/refs")
    assert refs == human_refs and refs["object_format"] == format
    indexed = {base64.b64decode(row["name_b64"]): row for row in refs["refs"]}
    assert indexed[b"HEAD"]["state"] == {
        "kind": "symbolic",
        "target_b64": base64.b64encode(b"refs/heads/trunk").decode(),
    }
    assert indexed[b"refs/tags/blob"]["object_kind"] == "blob"
    assert indexed[b"refs/tags/annotated"]["object_kind"] == "tag"
    assert indexed[b"refs/tags/annotated"]["peeled_oid"] == first
    assert app.client.get(metadata_path, headers=read_headers).json()["data"] == refs
    assert app.client.get(metadata_path).status_code == 401
    assert app.request("GET", metadata_path, expected=401).status_code == 401
    assert app.client.get(f"/api/v1/content/{project}/refs", headers=headers).status_code == 401
    assert inventory() == before_lookup
    response = app.client.get(status_path, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    status = response.json()["data"]
    assert (
        status["result"] == recorded["result"]
        and status["ref_request_sha256"] == recorded["request_sha256"]
    )
    assert (
        status["input_sha256"] is None
        and status["product"] is None
        and status["status"] == "committed"
    )
    assert app.client.get(status_path).status_code == 401
    assert (
        app.client.get(status_path, headers=read_headers).status_code == 404
    )  # Different principal.
    assert (
        app.request("GET", status_path, expected=401).status_code == 401
    )  # Human JWT is not Runtime authority.
    assert (
        app.client.get(
            f"/api/v1/content/{project}/operations/{recorded['request_key']}", headers=headers
        ).status_code
        == 401
    )
    assert (
        app.client.get(
            "/git/foreign.git/operations/" + recorded["request_key"], headers=headers
        ).status_code
        == 401
    )
    assert inventory() == before_lookup
    app.api("DELETE", f"/projects/{project}/git-credentials/{issued['id']}")
    assert app.client.get(status_path, headers=headers).status_code == 401
    assert app.client.get(metadata_path, headers=headers).status_code == 401
    assert cold.run("ls-remote", remote, check=False).returncode != 0
    assert (
        pg.value(
            f"SELECT target_oid FROM public.version_repository_refs WHERE project_id={literal(project)} AND name=decode({literal(b'refs/heads/trunk'.hex())},'hex')"
        )
        == first
    )
