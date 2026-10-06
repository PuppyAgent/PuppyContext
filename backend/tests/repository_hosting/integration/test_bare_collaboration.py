"""Concurrent real HTTP clients, formal native creation, PG and S3.

No server monkeypatches, synthetic grants or SQL publication. Partial HTTP
bodies hold two admitted requests open before their competing packs finish.
"""

import base64
import http.client
import json
import math
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from src.version_engine.adapters.git.protocol import pkt_line
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.integration.test_bare_repository_application import (
    bare_application as bare_application,
)
from tests.repository_hosting.integration.test_bare_repository_application import create_bare
from tests.repository_hosting.integration.test_docker_application import authorize_git

pytestmark = [pytest.mark.hosting_application, pytest.mark.hosting_s3]


def headers(secret):
    return {"Authorization": "Basic " + base64.b64encode(("x:" + secret).encode()).decode()}


def eventually(predicate, *, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "HTTP/lease cleanup deadline exceeded"
        time.sleep(0.05)


def seed(app, tmp_path, format):
    org, project, remote, secret = create_bare(app, branch="main", format=format)
    client = Git.init(tmp_path / "agent-a", format=format)
    authorize_git(client, secret)
    client.run("remote", "add", "origin", remote)
    base = client.commit({"shared.txt": b"base\n"})
    client.run("push", "origin", "main")
    return org, project, remote, secret, client, base


def issue_writer(app, project):
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
    return secret, issued["id"]


def receive_body(client, old, new, *, extra_ref=None):
    format = client.text("rev-parse", "--show-object-format")
    caps = f"report-status atomic object-format={format}"
    body = pkt_line(f"{old} {new} refs/heads/main\0{caps}\n".encode())
    if extra_ref:
        body += pkt_line(f"{'0' * len(old)} {new} {extra_ref}\n".encode())
    # A stock Git pack, not an in-process substitute for a client object graph.
    return body + b"0000" + client.run("pack-objects", "--all", "--stdout").stdout


@contextmanager
def pending_push(remote, secret, body):
    """Send a deliberately incomplete request; caller finishes or disconnects."""
    url = urlsplit(remote)
    assert url.scheme == "http" and url.hostname == "127.0.0.1"
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    connection.putrequest("POST", url.path + "/git-receive-pack")
    for key, value in headers(secret).items():
        connection.putheader(key, value)
    connection.putheader("Content-Type", "application/x-git-receive-pack-request")
    connection.putheader("Content-Length", str(len(body)))
    connection.endheaders()
    connection.send(body[:4])
    try:
        yield connection
    finally:
        connection.close()


def complete_push(connection, body):
    connection.send(body[4:])
    response = connection.getresponse()
    assert response.status == 200
    return response.read()


def leases(pg, project):
    return int(
        pg.value(
            f"SELECT count(*) FROM public.project_write_leases WHERE project_id={literal(project)}"
        )
    )


def clean_requests(pg, project):
    eventually(lambda: leases(pg, project) == 0)
    eventually(
        lambda: (
            pg.value(
                "SELECT count(*) FROM public.version_object_pins "
                f"WHERE project_id={literal(project)} AND purpose='read' AND state<>'released'"
            )
            == "0"
        )
    )


@pytest.mark.parametrize("format", ["sha1", "sha256"])
@pytest.mark.parametrize("overlap", [False, True], ids=["disjoint-files", "same-file-conflict"])
def test_bare_two_agents_atomic_cas_then_fetch_merge_push(
    bare_application, tmp_path, format, overlap
):
    app, pg = bare_application, Postgres()
    org, project, remote, secret_a, a, base = seed(app, tmp_path, format)
    # Independent credentials/clients, same repository and captured old OID.
    secret_b, _credential = issue_writer(app, project)
    b = Git.init(tmp_path / "agent-b", format=format)
    authorize_git(b, secret_b)
    b.run("remote", "add", "origin", remote)
    b.run("fetch", "origin", "main")
    b.run("reset", "--hard", "FETCH_HEAD")
    paths = ["shared.txt", "shared.txt"] if overlap else ["agent-a.txt", "agent-b.txt"]
    oids = [a.commit({paths[0]: b"agent-a\n"}), b.commit({paths[1]: b"agent-b\n"})]
    bodies = [
        receive_body(c, base, oid, extra_ref=f"refs/tags/agent-{i}")
        for i, (c, oid) in enumerate(zip((a, b), oids, strict=True))
    ]
    with (
        pending_push(remote, secret_a, bodies[0]) as first,
        pending_push(remote, secret_b, bodies[1]) as second,
    ):
        eventually(lambda: leases(pg, project) == 2)
        barrier = threading.Barrier(2)

        def finish(pair):
            barrier.wait(timeout=10)
            return complete_push(*pair)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, [(first, bodies[0]), (second, bodies[1])]))
    successes = [b"ok refs/heads/main\n" in result for result in results]
    assert sorted(successes) == [False, True], results
    winner = successes.index(True)
    loser = 1 - winner
    assert b"ng refs/heads/main " in results[loser]
    assert f"ng refs/tags/agent-{loser} ".encode() in results[loser]
    advertised = a.run("ls-remote", "origin").stdout
    assert f"{oids[winner]}\trefs/heads/main".encode() in advertised
    assert f"refs/tags/agent-{winner}".encode() in advertised
    assert f"refs/tags/agent-{loser}".encode() not in advertised
    clients = (a, b)
    assert clients[loser].text("rev-parse", "HEAD") == oids[loser]
    loser_client = clients[loser]
    loser_client.run("fetch", "origin")
    merged = loser_client.run(
        "merge", "--no-ff", "-m", "combine agents", "origin/main", check=False
    )
    if overlap:
        assert merged.returncode != 0
        assert loser_client.run("ls-files", "--unmerged").stdout
        # Conflict requires an explicit client resolution. CAS must never
        # silently convert a rejected write into a last-writer-wins publish.
        final = loser_client.commit({"shared.txt": b"agent-a\nagent-b\n"}, "resolve both agents")
    else:
        assert merged.returncode == 0, merged.stderr
        final = loser_client.text("rev-parse", "HEAD")
    loser_client.run("tag", "-a", "merged", "-m", "both contributions")
    loser_client.run("push", "--atomic", "origin", "main", "refs/tags/merged")
    clean_requests(pg, project)
    app.stop()
    app.start()
    cold = Git.init(tmp_path / "cold.git", bare=True, format=format)
    authorize_git(cold, secret_a)
    cold.run("fetch", remote, "+refs/*:refs/*")
    cold.run("fsck", "--full", "--strict")
    assert cold.text("rev-parse", "main") == final
    assert set(cold.text("show", "-s", "--format=%P", "main").split()) == set(oids)
    assert cold.text("rev-parse", "merged^{}") == final
    expected = (
        {"shared.txt": b"agent-a\nagent-b\n"}
        if overlap
        else {"shared.txt": b"base\n", "agent-a.txt": b"agent-a\n", "agent-b.txt": b"agent-b\n"}
    )
    for path, body in expected.items():
        assert cold.run("show", "main:" + path).stdout == body
    assert int(
        pg.value(
            "SELECT value FROM public.organization_usage_counters "
            f"WHERE org_id={literal(org)} AND metric='storage.logical_bytes'"
        )
    ) == sum(map(len, expected.values()))


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_bare_git_inflight_product_write_then_rebase(bare_application, tmp_path, format):
    app, pg = bare_application, Postgres()
    _org, project, remote, secret, git, base = seed(app, tmp_path, format)
    prefix = f"/content/{project}"
    revision = app.api("GET", prefix + "/ls")["repository_revision"]
    candidate = git.commit({"git.txt": b"Git agent\n"})
    body = receive_body(git, base, candidate)
    command = {
        "path": "web.txt",
        "content": "Web agent\n",
        "node_type": "file",
        "native": {"request_key": str(uuid.uuid4()), "repository_revision": revision},
    }
    with pending_push(remote, secret, body) as connection:
        eventually(lambda: leases(pg, project) == 1)
        saved = app.api("POST", prefix + "/write", json=command)
        assert saved["repository_transaction"]["status"] == "committed"
        assert b"ng refs/heads/main " in complete_push(connection, body)
    # Same request identity recovers the original ACK, without another write.
    assert app.api("POST", prefix + "/write", json=command) == saved
    app.request(
        "POST",
        "/api/v1" + prefix + "/write",
        expected=409,
        json={**command, "native": {**command["native"], "request_key": str(uuid.uuid4())}},
    )
    git.run("fetch", "origin")
    git.run("rebase", "origin/main")
    git.run("push", "origin", "main")
    final = git.text("rev-parse", "HEAD")
    assert app.api("GET", prefix + "/ls")["repository_revision"]["expected_oid"] == final
    for path in ("git.txt", "web.txt"):
        assert (
            app.api("GET", prefix + "/cat", params={"path": path})["content_text"]
            == (git.path / path).read_text()
        )
    clean_requests(pg, project)


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_bare_saturated_upload_disconnect_releases_slots_and_spools(
    bare_application, tmp_path, format
):
    app, pg = bare_application, Postgres()
    _org, project, remote, secret, git, base = seed(app, tmp_path, format)
    candidate = git.commit({"retry.txt": os.urandom(1024)})
    body = receive_body(git, base, candidate)
    before_spools = set(Path("/tmp").glob("puppyone-git-rpc-*"))
    with pending_push(remote, secret, body), pending_push(remote, secret, body):
        eventually(lambda: leases(pg, project) == 2)
        # A Project lease precedes Git admission. Do not let the probe itself
        # occupy a slot while the second upload is still authenticating.
        eventually(lambda: len(set(Path("/tmp").glob("puppyone-git-rpc-*")) - before_spools) == 2)
        response = app.client.get(
            remote + "/info/refs", headers=headers(secret), params={"service": "git-upload-pack"}
        )
        assert response.status_code == 503
        assert response.json()["message"] == "Git workers are busy"
        assert response.headers["retry-after"] == "1"
        assert app.client.get("/live").status_code == 200
        assert len(set(Path("/tmp").glob("puppyone-git-rpc-*")) - before_spools) == 2
    clean_requests(pg, project)
    eventually(lambda: set(Path("/tmp").glob("puppyone-git-rpc-*")) == before_spools)
    assert git.run("ls-remote", "origin", "main").stdout.startswith(base.encode())
    git.run("push", "origin", "main")
    assert git.run("ls-remote", "origin", "main").stdout.startswith(candidate.encode())
    clean_requests(pg, project)


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_bare_byte_names_atomic_refs_and_cold_roundtrip(bare_application, tmp_path, format):
    app = bare_application
    _org, _project, remote, secret, git, _base = seed(app, tmp_path, format)
    paths = ["中文/agent é.txt", "line\nbreak", b"raw-\xff".decode("utf-8", "surrogateescape")]
    oid = git.commit({name: name.encode("utf-8", "surrogateescape") for name in paths})
    branch, tag = "工作/agent-é", "版本/一"
    git.run("branch", branch)
    git.run("tag", "-a", tag, "-m", "unicode tag")
    git.run("push", "--atomic", "origin", "main", branch, "refs/tags/" + tag)
    cold = Git.init(tmp_path / "byte-cold.git", bare=True, format=format)
    authorize_git(cold, secret)
    cold.run("-c", "protocol.version=2", "fetch", remote, "+refs/*:refs/*")
    cold.run("fsck", "--full", "--strict")
    assert cold.text("rev-parse", branch) == oid
    assert cold.text("rev-parse", tag + "^{}") == oid
    for name in paths:
        assert cold.run("show", "main:" + name).stdout == name.encode("utf-8", "surrogateescape")


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_bare_revocation_during_upload_cannot_publish(bare_application, tmp_path, format):
    app, pg = bare_application, Postgres()
    _org, project, remote, _secret, git, base = seed(app, tmp_path, format)
    secret, credential = issue_writer(app, project)
    candidate = git.commit({"revoked.txt": b"must not publish with a revoked credential"})
    body = receive_body(git, base, candidate)
    with pending_push(remote, secret, body) as connection:
        eventually(lambda: leases(pg, project) == 1)
        app.api("DELETE", f"/projects/{project}/git-credentials/{credential}")
        connection.send(body[4:])
        response = connection.getresponse()
        content = response.read()
        # Authorization may fail at read-pin admission or final publication.
        # Neither path may acknowledge or install the candidate commit.
        assert response.status in (401, 403, 503)
        assert b"ok refs/heads/main" not in content
    clean_requests(pg, project)
    assert git.run("ls-remote", "origin", "main").stdout.startswith(base.encode())
    assert (
        app.client.get(
            remote + "/info/refs", headers=headers(secret), params={"service": "git-upload-pack"}
        ).status_code
        == 401
    )
    # The unrelated credential and repository remain usable.
    git.run("push", "origin", "main")
    assert git.run("ls-remote", "origin", "main").stdout.startswith(candidate.encode())


def test_bare_fetch_load_records_bounded_retry_and_latency(bare_application, tmp_path, request):
    """Small local baseline, not a hosted SLA or a 1000-client claim."""
    app, pg = bare_application, Postgres()
    _org, project, remote, secret, git, _base = seed(app, tmp_path, "sha1")
    body = os.urandom(1024**2)
    head = git.commit({"load.bin": body})
    git.run("push", "origin", "main")
    fetch_body = (
        pkt_line(f"want {head} object-format=sha1\n".encode()) + b"0000" + pkt_line(b"done\n")
    )
    samples = []
    for concurrency in (1, 2, 4, 8):
        clients = [Git.init(tmp_path / f"load-{concurrency}-{i}.git", bare=True) for i in range(16)]
        for client in clients:
            authorize_git(client, secret)

        def fetch(client):
            started = time.monotonic()
            busy = 0
            while True:
                response = app.client.post(
                    remote + "/git-upload-pack",
                    headers={
                        **headers(secret),
                        "Content-Type": "application/x-git-upload-pack-request",
                    },
                    content=fetch_body,
                )
                if response.status_code == 200:
                    break
                # Retry only explicit server backpressure, never integrity,
                # authentication, network or other protocol failures.
                assert response.status_code == 503
                assert response.json()["message"] == "Git workers are busy"
                assert response.headers["retry-after"] == "1"
                busy += 1
                assert time.monotonic() - started < 90, "bounded retry exhausted"
                time.sleep(1)
            latency = time.monotonic() - started
            # Each full HTTP response must be an intact stock-Git-readable
            # pack, even while other clients saturate the same service.
            assert response.content.startswith(pkt_line(b"NAK\n") + b"PACK")
            client.run("index-pack", "--stdin", "--strict", input=response.content[8:])
            client.run("update-ref", "refs/heads/main", head)
            assert client.text("rev-parse", "main") == head
            assert client.run("show", "main:load.bin").stdout == body
            client.run("fsck", "--full", "--strict")
            return latency, busy

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(fetch, clients))
        elapsed = time.monotonic() - started
        latencies = sorted(value[0] for value in results)
        rss = next(
            line
            for line in Path(f"/proc/{app.process.pid}/status").read_text().splitlines()
            if line.startswith("VmHWM:")
        )
        samples.append(
            {
                "clients": concurrency,
                "completed": len(results),
                "fetch_p50_seconds": latencies[math.ceil(len(latencies) * 0.5) - 1],
                "fetch_p95_seconds": latencies[math.ceil(len(latencies) * 0.95) - 1],
                "workflow_seconds": elapsed,
                "workflows_per_second": len(results) / elapsed,
                "busy_retries": sum(value[1] for value in results),
                "api_process_peak_rss_kib": int(rss.split()[1]),
            }
        )
        clean_requests(pg, project)
    assert not list((app.directory / "git-cache").rglob("HEAD"))
    evidence = {
        "profile": "owned Linux; one API process; 2 Git slots; 1 MiB random blob; 16 cold fetches per level",
        "latency_includes_busy_retry": True,
        "samples": samples,
    }
    Path("/evidence/bare-collaboration-load.json").write_text(json.dumps(evidence, indent=2) + "\n")
    request.node.user_properties.append(("load_baseline", json.dumps(evidence)))
