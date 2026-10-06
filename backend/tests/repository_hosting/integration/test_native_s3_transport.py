"""Stock HTTP Git -> production native transport -> real S3 and PostgREST.

The small ASGI fixture supplies an explicit admitted grant. Canonical credential
lookup/Project authorization is a separate gate, not claimed by this fixture.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI, Request

from src.version_engine.adapters.git.native_repository import NativeGitRepository
from tests.repository_hosting.harness.conformance import remote_snapshot
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http import clone_client
from tests.repository_hosting.integration.test_ref_transaction_service import grant
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)
from tests.repository_hosting.transport.test_git_conformance import WORKFLOWS
from tests.version_engine.test_write_engine import _serve_git_app

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture


@pytest.fixture
def native_http(publication, tmp_path):
    _pg, auth, _s3, _db, _backend, service, original, _oid, _prepare = publication
    transport = NativeGitRepository(service)
    admitted = grant(auth.project)
    app = FastAPI()
    @app.get("/repo.git/info/refs")
    async def info(service: str, request: Request):
        return await asyncio.to_thread(transport.info_refs, admitted, service, protocol=request.headers.get("git-protocol", ""))
    @app.post("/repo.git/{command}")
    async def rpc(command: str, request: Request):
        spool = tmp_path / ("rpc-" + uuid.uuid4().hex)
        spool.write_bytes(await request.body())
        try:
            if command == "git-receive-pack":
                return await asyncio.to_thread(transport.receive, admitted, spool)
            assert command == "git-upload-pack"
            return await asyncio.to_thread(transport.upload, admitted, spool, protocol=request.headers.get("git-protocol", ""))
        finally:
            spool.unlink()
    with _serve_git_app(app) as address:
        yield original, address + "/repo.git", auth, transport


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_stock_push_durable_native_refs_and_cold_clone(native_http, tmp_path):
    client, remote, auth, _transport = native_http
    client.run("remote", "add", "origin", remote)
    oid = client.text("rev-parse", "HEAD")
    client.run("push", "origin", "main")
    assert auth.state()["oid"] == oid
    # Native transport reads canonical objects directly; no local repository
    # can keep an acknowledged but unpersisted object alive.
    client.run("clone", "--mirror", remote, tmp_path / "cold.git")
    cold = Git(tmp_path / "cold.git")
    cold.run("fsck", "--full", "--strict")
    assert cold.refs() == {name: value for name, value in client.refs().items() if not name.startswith("refs/remotes/")}
    assert cold.objects() == client.objects()


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_native_http_atomic_refs_tags_rewrites_and_delete(native_http, tmp_path):
    client, remote, _auth, _transport = native_http
    client.run("remote", "add", "origin", remote)
    client.run("push", "origin", "main")
    old = client.text("rev-parse", "HEAD")
    client.run("tag", "-a", "annotated", "-m", "raw tag")
    client.run("branch", "feature")
    client.run("push", "--atomic", "origin", "main", "feature", "refs/tags/annotated")
    client.commit({"second": b"rewrite later"})
    client.run("push", "origin", "main")
    client.run("reset", "--hard", old)
    client.run("push", "--force", "origin", "main")
    client.run("push", "origin", "--delete", "feature", "annotated")
    client.run("branch", "-D", "feature")
    client.run("tag", "-d", "annotated")
    client.run("clone", "--mirror", remote, tmp_path / "cold.git")
    cold = Git(tmp_path / "cold.git")
    cold.run("fsck", "--full", "--strict")
    assert cold.refs() == {"refs/heads/main": old}
    assert cold.run("cat-file", "commit", old).stdout == client.run("cat-file", "commit", old).stdout


@pytest.fixture
def native_reference(native_http, workflow, tmp_path):
    source, _remote, _auth, _transport = native_http
    native = Git.init(tmp_path / "oracle.git", bare=True)
    native.run("config", "uploadpack.allowFilter", "true")
    native.run("config", "uploadpack.allowAnySHA1InWant", "true")
    source.run("push", native.path.as_uri(), "main")
    client = clone_client(native.path.as_uri(), tmp_path / "oracle-client", source)
    workflow.execute(client)
    return remote_snapshot(source, native.path.as_uri(), tmp_path / "oracle-readback.git")


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda workflow: workflow.name)
def test_workflow_on_real_s3_and_pg_matches_native(native_http, workflow, native_reference, tmp_path, request):
    source, remote, _auth, _transport = native_http
    request.node.user_properties.extend([
        ("git_workflow", workflow.name), ("git_profile", "native-full-project-sha1"),
        ("storage_evidence", "real owned Supabase S3-compatible service + PostgREST/PG; explicit admitted grant"),
    ])
    source.run("push", remote, "main")
    client = clone_client(remote, tmp_path / "hosted-client", source)
    workflow.execute(client)
    actual = remote_snapshot(source, remote, tmp_path / "hosted-readback.git")
    assert actual.refs == native_reference.refs
    assert actual.head == native_reference.head
    assert actual.objects == native_reference.objects
