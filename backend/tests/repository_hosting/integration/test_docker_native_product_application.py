"""Real JWT -> Product HTTP -> native PG/S3 -> cold Git and read-only replay.

Owner-installed enrollment/entitlements are still synthetic. No migration,
Scope, worker, billing provider or Desktop acceptance is implied.
"""
import base64
import secrets
import uuid

import pytest

from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.supabase_api import SupabaseAPI
from tests.repository_hosting.integration.test_docker_application import authorize_git
from tests.repository_hosting.integration.test_docker_native_git_application import (
    application as application_fixture,
)
from tests.repository_hosting.integration.test_docker_native_git_application import (
    enroll_empty_native,
)

pytestmark = pytest.mark.hosting_application
application = application_fixture


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_docker_native_product_commands_cold_git_and_read_only_replay(application, tmp_path, format, request):
    app, pg = application, Postgres()
    app.profile = "native Git/selected Product reads and writes; synthetic enrollment; Scope/workers/migration not accepted"
    org = app.api("POST", "/organizations/", expected=201, json={"name": "Native Product"})["id"]
    project = app.api("POST", "/projects/", expected=201,
                      headers={"Idempotency-Key": str(uuid.uuid4())},
                      json={"name": "Native Product", "org_id": org})["id"]
    credential = "pwg_" + secrets.token_urlsafe(32)
    issued = app.api("POST", f"/projects/{project}/git-credentials", expected=201,
                     headers={"Idempotency-Key": str(uuid.uuid4())}, json={
                         "target": {"kind": "project_root", "project_id": project},
                         "mode": "rw", "credential": credential,
                     })
    enroll_empty_native(pg, project, org, format)
    # A real non-creator editor can later be downgraded. The Project creator
    # must retain Admin membership; do not disable that shipped SQL invariant.
    editor_auth = SupabaseAPI(app.env)
    request.addfinalizer(editor_auth.close)
    editor_auth.authenticate()
    editor = editor_auth.request("GET", "/auth/v1/user", role="authenticated").json()["id"]
    pg.sql(f"INSERT INTO public.org_members(org_id,user_id,role) VALUES({literal(org)},{literal(editor)},'member');"
           f"INSERT INTO public.project_members(org_id,project_id,user_id,role) VALUES({literal(org)},{literal(project)},{literal(editor)},'editor')")
    owner_auth, app.auth = app.auth, editor_auth
    def restore_auth():
        app.auth = owner_auth
    request.addfinalizer(restore_auth)
    prefix = f"/content/{project}"
    def revision():
        return app.api("GET", prefix+"/ls")["repository_revision"]
    def command(action, data, base=None, key=None, expected=200, byte_paths=None):
        body = {**data, "native": {"request_key": key or str(uuid.uuid4()),
                                  "repository_revision": revision() if base is None else base,
                                  "byte_paths": byte_paths or {}}}
        response = app.request("POST", "/api/v1"+prefix+"/"+action, json=body, expected=expected)
        return response.json().get("data"), body
    base = revision()
    first, original = command("write", {"path": "one.txt", "content": "native\n", "node_type": "file"}, base)
    assert first["repository_transaction"]["status"] == "committed"
    assert first["commit_id"] and len(first["commit_id"]) == (40 if format == "sha1" else 64)
    after_first = revision()
    command("write", {"path": "too-big", "content": "x"*65, "node_type": "file"}, expected=413)
    assert revision() == after_first
    command("write", {"path": "one.txt", "content": "lost", "node_type": "file"}, base, expected=409)
    noop, _ = command("write", {"path": "one.txt", "content": "native\n", "node_type": "file"})
    assert noop["commit_id"] == first["commit_id"] and revision() == after_first
    raw_path = b"raw-\xff\\name"
    raw_b64 = base64.b64encode(raw_path).decode("ascii")
    command("bulk-write", {"files": [
        {"path": "batch.txt", "content": "batch\n", "node_type": "file"},
        {"path": "second.txt", "content": "second\n", "node_type": "file"},
        {"path": "", "content": "raw\n", "node_type": "file"},
    ]}, byte_paths={"files/2/path": raw_b64})
    command("mkdir", {"path": "folder"})
    command("mv", {"old_path": "second.txt", "new_path": "folder/renamed.txt"})
    final, _ = command("rm", {"path": "batch.txt"})
    final_revision = revision()
    assert final_revision["expected_oid"] == final["commit_id"]
    assert app.api("GET", prefix+"/cat", params={"path": "one.txt"})["content_text"] == "native\n"
    assert app.api("GET", prefix+"/cat", params={"path": "folder/renamed.txt"})["content_text"] == "second\n"
    assert app.request("GET", "/api/v1"+prefix+"/raw", params={"path_bytes_b64": raw_b64}).content == b"raw\n"
    app.request("POST", "/api/v1"+prefix+"/write", json=original, token=False, expected=401)
    # Product writes must not use transport materialization, even temporarily.
    assert not list((app.directory / "git-cache").rglob("HEAD"))
    assert pg.value(f"SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}") == "0"
    assert pg.value(f"SELECT value FROM public.organization_usage_counters WHERE org_id={literal(org)} AND metric='storage.logical_bytes'") == "18"
    product_events = pg.value(f"SELECT count(*) FROM public.version_ref_events WHERE project_id={literal(project)} AND payload ? 'product'")
    assert product_events == "5"  # no-op is a result, not a changed-ref event
    app.stop()
    app.start()
    cold = Git.init(tmp_path / "product-cold.git", bare=True, format=format)
    authorize_git(cold, credential)
    cold.run("fetch", issued["remote"]["url"], "+refs/*:refs/*")
    assert cold.text("rev-parse", "refs/heads/trunk") == final["commit_id"]
    assert cold.run("show", final["commit_id"]+":folder/renamed.txt").stdout == b"second\n"
    assert cold.run("show", first["commit_id"]+":one.txt").stdout == b"native\n"
    assert cold.run("show", final["commit_id"]+":"+raw_path.decode("utf-8", "surrogateescape")).stdout == b"raw\n"
    cold.run("fsck", "--full", "--strict")
    # Lose write permission, current entitlement and file admission. The exact
    # acknowledged response is still a read and must not allocate/charge again.
    pg.sql(f"UPDATE public.project_members SET role='viewer' WHERE project_id={literal(project)} AND user_id={literal(editor)};"
           f"UPDATE public.version_repository_file_policies SET initialized=false WHERE project_id={literal(project)};"
           f"DELETE FROM public.organization_entitlements WHERE org_id={literal(org)}")
    def inventory():
        return [pg.value(f"SELECT count(*) FROM public.{table} WHERE project_id={literal(project)}") for table in (
            "project_write_leases", "version_object_pins", "version_ref_transactions", "version_ref_events")]
    before = inventory()
    assert app.api("POST", prefix+"/write", json=original) == first
    assert inventory() == before
    modified = {**original, "content": "different input"}
    app.request("POST", "/api/v1"+prefix+"/write", json=modified, expected=409)
    changed_key = {**original, "native": {**original["native"], "request_key": str(uuid.uuid4())}}
    app.request("POST", "/api/v1"+prefix+"/write", json=changed_key, expected=403)
    assert inventory() == before
    # This explicit read legitimately allocates its own read pin. Keep it after
    # the replay/denial allocation assertions rather than blaming that pin on replay.
    assert revision() == final_revision
    pg.sql(f"DELETE FROM public.org_members WHERE org_id={literal(org)} AND user_id={literal(editor)}")
    app.request("POST", "/api/v1"+prefix+"/write", json=original, expected=404)
