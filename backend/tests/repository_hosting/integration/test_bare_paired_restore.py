"""Restore an explicit quiescent PG/S3 recovery point into independent stores.

The restored reader uses SQL transport instead of PostgREST (not an Auth restore
test). Creation and pre-backup ACK use the actual application and credentials.
"""

import asyncio
import copy
import hashlib
import json
import subprocess
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI, Request

from src.version_engine.adapters.git.native_repository import NativeGitRepository
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.write_engine.ref_transaction import RefTransactionService
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_bare_repository_application import (
    bare_application as bare_application,
)
from tests.repository_hosting.integration.test_bare_repository_application import create_bare
from tests.repository_hosting.integration.test_docker_application import authorize_git
from tests.repository_hosting.integration.test_ref_transaction_service import SQLClient, grant
from tests.version_engine.test_write_engine import _serve_git_app

pytestmark = [pytest.mark.hosting_application, pytest.mark.hosting_s3]


class RestoredSQLClient(SQLClient):
    def table(self, name):
        assert name == "version_object_locations"
        pg = self.pg

        class Locations:
            def __init__(self):
                self.filters = []

            def select(self, _columns):
                return self

            def eq(self, column, value):
                assert column in ("project_id", "object_hash")
                self.filters.append(column + "=" + literal(value))
                return self

            def in_(self, column, values):
                assert column in ("object_hash", "object_id")
                self.filters.append(column + " IN (" + ",".join(map(literal, values)) + ")")
                return self

            def execute(self):
                value = pg.value(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t)),'[]') FROM public.version_object_locations t WHERE "
                    + " AND ".join(self.filters)
                )
                return SimpleNamespace(data=json.loads(value))

        return Locations()


def test_bare_paired_pg_s3_restore_keeps_acknowledged_recovery_point(
    bare_application, tmp_path, request
):
    app, pg = bare_application, Postgres()
    _org, project, remote, secret = create_bare(app, branch="main")
    source = Git.init(tmp_path / "source")
    authorize_git(source, secret)
    acknowledged = source.commit({"restore.txt": b"ACK before recovery point"})
    source.run("tag", "-a", "recovery", "-m", "recorded recovery point")
    source.run("push", "--atomic", remote, "main", "refs/tags/recovery")
    expected_objects = source.objects()
    expected_refs = source.refs()
    dump = tmp_path / "recovery.dump"
    with owned_s3() as (s3, api):
        # Quiesce this owned application's only writer before pairing snapshots.
        app.stop()
        try:
            result = subprocess.run(
                [
                    "pg_dump",
                    pg.url,
                    "--format=custom",
                    "--exclude-schema=graphql_public",
                    "-f",
                    str(dump),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr
            dump.chmod(0o600)
            # Use the actual canonical layout; the manifest includes every key
            # for the selected Project, not a hand-picked reachable subset.
            all_keys = [
                entry["Key"]
                for page in s3.client.get_paginator("list_objects_v2").paginate(
                    Bucket=s3.bucket_name
                )
                for entry in page.get("Contents", [])
                if project in entry["Key"].split("/")
            ]
            assert all_keys
            objects = {
                key: s3.client.get_object(Bucket=s3.bucket_name, Key=key)["Body"].read()
                for key in all_keys
            }
            ref_point = pg.value(
                f"SELECT public.get_version_repository_snapshot({literal(project)})"
            )
        finally:
            app.start()
        later = source.commit({"later.txt": b"ACK after recovery point"})
        source.run("push", remote, "main")
        assert later != acknowledged
        bucket = s3.bucket_name + "-restore-" + uuid.uuid4().hex[:8]
        response = api.request(
            "POST", "/storage/v1/bucket", json={"id": bucket, "name": bucket, "public": False}
        )
        assert response.status_code in (200, 201)
        for key, body in objects.items():
            s3.client.put_object(Bucket=bucket, Key=key, Body=body)
        restored_s3 = copy.copy(s3)
        restored_s3.bucket_name = bucket
        try:
            with pg.empty_database() as restored_pg:
                # Supabase system functions include privileged SET parameters;
                # restore with the owned cluster administrator, preserving owners/ACLs.
                parsed = urlparse(restored_pg.url)
                restore_url = parsed._replace(
                    netloc="supabase_admin:"
                    + parsed.password
                    + "@"
                    + parsed.hostname
                    + ":"
                    + str(parsed.port)
                ).geturl()
                # pg_dump still emits extension-member ACLs for the excluded
                # provider-owned GraphQL schema. Exclude only those TOC entries;
                # retain every native table, function, owner, trigger and ACL.
                toc = subprocess.run(
                    ["pg_restore", "--list", str(dump)], capture_output=True, text=True, timeout=30
                )
                assert toc.returncode == 0
                selected = tmp_path / "recovery.toc"
                selected.write_text(
                    "\n".join(
                        line for line in toc.stdout.splitlines() if "graphql_public" not in line
                    )
                    + "\n"
                )
                result = subprocess.run(
                    [
                        "pg_restore",
                        "--exit-on-error",
                        "--use-list",
                        str(selected),
                        "-d",
                        restore_url,
                        str(dump),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                assert result.returncode == 0, result.stderr
                assert json.loads(
                    restored_pg.value(
                        f"SELECT public.get_version_repository_snapshot({literal(project)})"
                    )
                ) == json.loads(ref_point)
                client = RestoredSQLClient(restored_pg)
                backend = S3StorageBackend(
                    restored_s3,
                    project,
                    supabase=SimpleNamespace(client=client),
                    allow_deferred_namespace_reads=False,
                )
                service = RefTransactionService(
                    RefAuthorityRepository(client), backend, project_id=project
                )
                transport = NativeGitRepository(service)
                restored_app = FastAPI()

                @restored_app.get("/repo.git/info/refs")
                async def info(service: str, request: Request):
                    return await asyncio.to_thread(
                        transport.info_refs,
                        grant(project),
                        service,
                        protocol=request.headers.get("git-protocol", ""),
                    )

                @restored_app.post("/repo.git/git-upload-pack")
                async def fetch(request: Request):
                    path = tmp_path / ("restore-request-" + uuid.uuid4().hex)
                    path.write_bytes(await request.body())
                    try:
                        return await asyncio.to_thread(
                            transport.upload,
                            grant(project),
                            path,
                            protocol=request.headers.get("git-protocol", ""),
                        )
                    finally:
                        path.unlink()

                with _serve_git_app(restored_app) as address:
                    source.run(
                        "clone", "--mirror", address + "/repo.git", tmp_path / "restored.git"
                    )
                cold = Git(tmp_path / "restored.git")
                cold.run("fsck", "--full", "--strict")
                assert cold.refs() == expected_refs
                assert cold.objects() == expected_objects
                assert cold.run("cat-file", "-e", later, check=False).returncode != 0
                manifest = {
                    "excluded_provider_schemas": ["graphql_public"],
                    "pg_dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                    "objects": {
                        key: hashlib.sha256(body).hexdigest() for key, body in objects.items()
                    },
                    "recovery_head": acknowledged,
                    "excluded_later_head": later,
                    "restore": "independent PostgreSQL database and independent S3 bucket; cold stock Git mirror/fsck",
                }
                from pathlib import Path

                Path("/evidence/bare-recovery-point.json").write_text(
                    json.dumps(manifest, indent=2) + "\n"
                )
                request.node.user_properties.append(("recovery_point", acknowledged))
        finally:
            for key in objects:
                s3.client.delete_object(Bucket=bucket, Key=key)
            assert api.request("DELETE", "/storage/v1/bucket/" + bucket).status_code == 200
