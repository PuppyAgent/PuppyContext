"""Fixed-budget transfers through real creation/auth/HTTP/PG/S3."""

import base64
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.integration.test_bare_repository_application import (
    bare_application as bare_application,
)
from tests.repository_hosting.integration.test_bare_repository_application import create_bare
from tests.repository_hosting.integration.test_docker_application import authorize_git

pytestmark = [pytest.mark.hosting_application, pytest.mark.hosting_s3]


def test_bare_limits_large_object_concurrent_cold_fetch_and_rejection(bare_application, tmp_path):
    app, pg = bare_application, Postgres()
    _org, project, remote, secret = create_bare(app, branch="main")
    auth = {"Authorization": "Basic " + base64.b64encode(("x:" + secret).encode()).decode()}
    health = app.client.get(f"/git/{project}.git/health", headers=auth).json()["data"]
    assert health["capabilities"]["scope_git"] is False
    assert health["limits"]["concurrent_workers_per_process"] == 2
    source = Git.init(tmp_path / "source")
    authorize_git(source, secret)
    body = os.urandom(8 * 1024**2)  # exact declared file limit, not a tiny compressible stand-in
    accepted = source.commit({"at-limit.bin": body})
    source.run("push", remote, "main")

    def cold_clone(index):
        source.run(
            "-c",
            "http.extraHeader=Authorization: " + auth["Authorization"],
            "clone",
            "--mirror",
            remote,
            tmp_path / f"cold-{index}.git",
        )
        cold = Git(tmp_path / f"cold-{index}.git")
        cold.run("fsck", "--full", "--strict")
        assert cold.run("show", "main:at-limit.bin").stdout == body

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(cold_clone, (1, 2)))
    source.commit({"over-limit.bin": b"x" * (8 * 1024**2 + 1)})
    assert source.run("push", remote, "main", check=False).returncode != 0
    assert (
        pg.value(
            f"SELECT target_oid FROM public.version_repository_refs WHERE project_id={literal(project)} AND name=convert_to('refs/heads/main','UTF8')"
        )
        == accepted
    )
    assert pg.value(
        f"SELECT accounted_bytes FROM public.version_repository_billing WHERE project_id={literal(project)}"
    ) == str(len(body))
    # Request size is checked before spooling or invoking Git, even if a client
    # supplies a forged oversized Content-Length without sending the body.
    import asyncio

    from fastapi import HTTPException, Request

    from src.version_engine.adapters.git.execution import MAX_PACK_BYTES
    from src.version_engine.entrypoints.git.router import _spool_git_request_body

    request = Request(
        {"type": "http", "headers": [(b"content-length", str(MAX_PACK_BYTES + 1).encode())]}
    )
    with pytest.raises(HTTPException):
        asyncio.run(_spool_git_request_body(request, max_body_bytes=MAX_PACK_BYTES))


def test_bare_long_chunked_receive_renews_real_project_lease(bare_application, tmp_path):
    import time

    from src.version_engine.adapters.git.protocol import pkt_line

    app, pg = bare_application, Postgres()
    _org, project, remote, secret = create_bare(app, branch="main")
    source = Git.init(tmp_path / "source")
    oid = source.commit({"long-upload": b"actual delayed HTTP body"})
    payload = (
        pkt_line(
            ("0" * 40 + " " + oid + " refs/heads/main\0report-status side-band-64k\n").encode()
        )
        + b"0000"
        + source.run("pack-objects", "--all", "--stdout").stdout
    )
    observed = []

    def chunks():
        yield payload[:4]
        deadline = time.monotonic() + 10
        query = f"SELECT max(extract(epoch from expires_at)) FROM public.project_write_leases WHERE project_id={literal(project)}"
        while time.monotonic() < deadline:
            first = pg.value(query)
            if first:
                break
            time.sleep(0.1)
        assert first
        time.sleep(45)  # production heartbeat interval is 40s for the 120s lease
        renewed = pg.value(query)
        assert renewed and float(renewed) > float(first) + 30
        observed.append(True)
        yield payload[4:]

    auth = {
        "Authorization": "Basic " + base64.b64encode(("x:" + secret).encode()).decode(),
        "Content-Type": "application/x-git-receive-pack-request",
    }
    response = app.client.post(
        remote + "/git-receive-pack", headers=auth, content=chunks(), timeout=90
    )
    assert response.status_code == 200 and observed
    authorize_git(source, secret)
    assert oid.encode() in source.run("ls-remote", remote, "refs/heads/main").stdout
    cleanup_deadline = time.monotonic() + 5
    while (
        pg.value(
            f"SELECT count(*) FROM public.project_write_leases WHERE project_id={literal(project)}"
        )
        != "0"
    ):
        assert time.monotonic() < cleanup_deadline, "request lease remained after response cleanup"
        time.sleep(0.05)
