"""Actual immutable operator artifact -> owned Docker PG/S3 -> native engine.

Synthetic legacy fixtures mirror observed formats; no hosted data/credentials.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from supabase import ClientOptions, create_client

from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.runner import DataMigrationRunner
from src.platform.authorization.models import (
    GrantSource,
    ProjectCapability,
    ProjectGrant,
    ProjectRole,
)
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.git.native_repository import NativeGitRepository
from src.version_engine.derived.object_gc import run_git_object_gc
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http_server import _serve_git_app
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)
from tests.repository_hosting.integration.test_s3_publication import (
    test_gc_preserves_verified_refs_and_reclaims_actual_s3_orphan as assert_unmigrated_gc,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.hosting_migration]
publication = publication_fixture
ROOT = Path(__file__).resolve().parents[4]
ARTIFACT = ROOT / "supabase/data_migrations/20261007_native_repository_inventory"
spec = importlib.util.spec_from_file_location("immutable_native_migration", ARTIFACT / "run.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

rollout_spec = importlib.util.spec_from_file_location(
    "scoped_native_rollout", ROOT / "supabase/data_migrations/20261008_qubits_agent_project/run.py"
)
rollout = importlib.util.module_from_spec(rollout_spec)
rollout_spec.loader.exec_module(rollout)


def fixture(pg):
    project = pg.create_project()
    row = json.loads(
        pg.value(
            "SELECT jsonb_build_object('org',org_id,'user',created_by) FROM public.projects WHERE id="
            + literal(project)
        )
    )
    pg.sql(
        "INSERT INTO public.organization_entitlements(org_id,source,source_revision,payload_hash,entitlements) VALUES("
        + ",".join(
            map(
                literal,
                (
                    row["org"],
                    "puppypay",
                    1,
                    "a" * 64,
                    {
                        "limits": {
                            "storage.max_bytes": 10000000,
                            "upload.max_single_file_bytes": 1000000,
                        }
                    },
                ),
            )
        )
        + ");"
    )
    grant = ProjectGrant(
        project,
        row["org"],
        row["user"],
        ProjectRole.ADMIN,
        GrantSource.PROJECT_MEMBER,
        frozenset({ProjectCapability.CONTENT_READ, ProjectCapability.CONTENT_WRITE}),
    )
    return SimpleNamespace(pg=pg, project=project, org=row["org"], user=row["user"], grant=grant)


def operator(a, s3, apply=True):
    return migration.Migration(
        migration.Database(a.pg.url), s3.client, s3.bucket_name, a.project, apply=apply
    )


def upload_git(a, s3, git, layout="loose"):
    objects = git.objects()
    for oid, (kind, body) in objects.items():
        raw = migration.loose(kind, body)[1]
        key = f"mut/{a.project}/objects/{oid[:2]}/{oid[2:]}"
        offset = 0
        if layout == "bundle":
            key = f"mut/{a.project}/object-bundles/fixture.pob"
            # One bundle per object keeps range semantics obvious.
            key = key.replace("fixture", oid)
            offset = 13
            s3.client.put_object(Bucket=s3.bucket_name, Key=key, Body=b"bundle-prefix" + raw)
        elif layout == "chunk":
            root = f"mut/{a.project}/object-bundles/chunked/{oid[:2]}/{oid}"
            key = root + ".json"
            s3.client.put_object(Bucket=s3.bucket_name, Key=root + "/part-000001", Body=raw)
            manifest = {
                "version": 1,
                "object_id": oid,
                "size_bytes": len(raw),
                "chunks": [
                    {"key": root + "/part-000001", "offset_bytes": 0, "size_bytes": len(raw)}
                ],
            }
            s3.client.put_object(Bucket=s3.bucket_name, Key=key, Body=json.dumps(manifest).encode())
            key = "chunked:" + key
        else:
            s3.client.put_object(Bucket=s3.bucket_name, Key=key, Body=raw)
        if layout != "loose":
            a.pg.sql(
                "INSERT INTO public.version_object_locations(project_id,object_id,pack_key,offset_bytes,size_bytes) VALUES("
                + ",".join(map(literal, (a.project, oid, key, offset, len(raw))))
                + ");"
            )
    head, tree = git.text("rev-parse", "HEAD"), git.text("rev-parse", "HEAD^{tree}")
    a.pg.sql(
        "UPDATE public.projects SET version_root_hash="
        + literal(tree)
        + " WHERE id="
        + literal(a.project)
        + ";"
        "INSERT INTO public.version_scope_state(project_id,scope_path,scope_hash,head_commit_id) VALUES("
        + ",".join(map(literal, (a.project, "", tree, head)))
        + ");"
        "INSERT INTO public.version_commits(project_id,root_hash,who,message,commit_id) VALUES("
        + ",".join(map(literal, (a.project, tree, "test", "retained original", head)))
        + ");"
    )
    return head, tree


def upload_raw(a, s3):
    blob, tree = "1" * 16, "2" * 16
    body = b"legacy text\n"
    for oid, raw in (
        (blob, body),
        (
            tree,
            json.dumps({"a.txt": ["B", blob], "copy.txt": {"type": "file", "hash": blob}}).encode(),
        ),
    ):
        s3.client.put_object(
            Bucket=s3.bucket_name, Key=f"mut/{a.project}/objects/{oid[:2]}/{oid[2:]}", Body=raw
        )
    a.pg.sql(
        "UPDATE public.projects SET version_root_hash="
        + literal(tree)
        + " WHERE id="
        + literal(a.project)
        + ";"
    )
    return tree, body


def activate(a):
    return a.pg.value(
        "SELECT public.activate_native_repository_migrations(" + literal(a.org) + ");"
    )


def test_native_inventory_00_portable_runner_and_cold_native_write(tmp_path, monkeypatch):
    """Run FIRST in this dedicated suite: global verify requires all projects."""
    pg = Postgres()
    a, b = fixture(pg), fixture(pg)
    git = Git.init(tmp_path / "original")
    git.commit({"before.txt": b"before"})
    git.commit({"after.txt": b"after"})
    with owned_s3() as (s3, api), httpx.Client(timeout=30, trust_env=False) as http:
        head, tree = upload_git(a, s3, git, "bundle")
        _, raw_body = upload_raw(b, s3)
        db = PsqlClient(pg.url)
        # The immutable runner intentionally strips LD_LIBRARY_PATH. Use the
        # image's system libpq client (SQL protocol compatible with PG17), not
        # the separately bundled pg_dump tools requiring a loader override.
        env = dict(
            os.environ,
            DATA_MIGRATION_DATABASE_URL=pg.url,
            NATIVE_MIGRATION_WRITERS_DRAINED="yes",
            PATH="/usr/bin:" + os.environ["PATH"],
        )
        runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment=env)
        runner.run(migration.ARTIFACT)
        receipt = db.receipt(migration.ARTIFACT)
        assert receipt and receipt["artifact_checksum"]
        runner.run(migration.ARTIFACT)
        runner.verify_external_state(migration.ARTIFACT)
        assert db.receipt(migration.ARTIFACT) == receipt
        assert (
            pg.value(
                "SELECT state FROM public.version_repository_migrations WHERE project_id="
                + literal(a.project)
            )
            == "activated"
        )
        assert (
            pg.value("SELECT version_root_hash FROM public.projects WHERE id=" + literal(a.project))
            == tree
        )
        assert int(
            pg.value(
                "SELECT accounted_bytes FROM public.version_repository_billing WHERE project_id="
                + literal(b.project)
            )
        ) == 2 * len(raw_body)
        sdk = create_client(
            str(api.client.base_url),
            api.headers("service_role")["apikey"],
            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
        )
        manager = VersionRepoManager(s3, SimpleNamespace(client=sdk))
        service = manager.get_native_service(a.project)
        assert service is not None
        with repository_snapshot(
            service.control, service.backend, a.grant, project_id=a.project
        ) as snapshot:
            assert snapshot.revision().commit_oid == head
            assert snapshot.revision().tree_oid == tree
        ClosureVerifier(service.backend).verify({head: "commit"})
        # Stock Git independently verifies the complete migrated graph.
        cold = Git.init(tmp_path / "cold.git", bare=True)
        for oid, (kind, body) in git.objects().items():
            assert migration.decode(service.backend.get_durable(oid), oid) == (kind, body)
            assert (
                cold.run("hash-object", "-w", "-t", kind, "--stdin", input=body)
                .stdout.strip()
                .decode()
                == oid
            )
        cold.run("update-ref", "refs/heads/main", head)
        cold.run("fsck", "--full", "--strict")
        transport = NativeGitRepository(service)
        app = FastAPI()

        @app.get("/repo.git/info/refs")
        async def info(service: str, request: Request):
            return await asyncio.to_thread(
                transport.info_refs,
                a.grant,
                service,
                protocol=request.headers.get("git-protocol", ""),
            )

        @app.post("/repo.git/git-upload-pack")
        async def upload(request: Request):
            spool = tmp_path / ("negotiation-" + uuid.uuid4().hex)
            spool.write_bytes(await request.body())
            try:
                return await asyncio.to_thread(
                    transport.upload,
                    a.grant,
                    spool,
                    protocol=request.headers.get("git-protocol", ""),
                )
            finally:
                spool.unlink()

        with _serve_git_app(app) as address:
            git.run("clone", "--mirror", address + "/repo.git", tmp_path / "http-cold.git")
        fetched = Git(tmp_path / "http-cold.git")
        fetched.run("fsck", "--full", "--strict")
        assert fetched.text("rev-parse", "HEAD") == head
        assert fetched.objects() == git.objects()
        gc = run_git_object_gc(manager.get_gc_repo(a.project), dry_run=False, retention_seconds=0)
        assert gc.sweep_skipped_for_safety and gc.deleted_count == 0
        assert (
            "migration_source_retention_required"
            in pg.sql(
                "SET ROLE service_role; SELECT public.begin_version_repository_gc("
                + literal(a.project)
                + ","
                + literal(str(uuid.uuid4()))
                + ");",
                check=False,
            ).stderr
        )
        # First post-migration write crosses current lease, billing/capacity and
        # publication code; no test-only metadata enrollment after migration.
        new = git.commit({"new.txt": b"native write"})

        async def publish():
            async with ProjectWriteLease(
                a.project, "migration-test", repository=ProjectWriteLeaseRepository(sdk)
            ):
                current = manager.get_native_service(a.project)

                def prepare():
                    for oid, (kind, body) in git.objects().items():
                        current.backend.put(oid, migration.loose(kind, body)[1])

                return await asyncio.to_thread(
                    current.submit,
                    a.grant,
                    request_key=str(uuid.uuid4()),
                    generation=2,
                    edits=[RefEdit(b"refs/heads/main", RefState(oid=head), RefState(oid=new))],
                    roots={new: "commit"},
                    prepare=prepare,
                )

        assert asyncio.run(publish())["status"] == "committed"
        runner.verify_external_state(migration.ARTIFACT)  # New writes do not invalidate completion.


@pytest.mark.parametrize("layout", ["loose", "bundle", "chunk"])
def test_native_inventory_formats_retry_and_source_retention(tmp_path, layout):
    a = fixture(Postgres())
    git = Git.init(tmp_path / "git")
    git.commit({"file": b"roundtrip", "other": b"binary\x00\xff"})
    with owned_s3() as (s3, _):
        head, tree = upload_git(a, s3, git, layout)
        plan = operator(a, s3, False).prepare()
        assert plan["state"] == "planned"
        assert (
            a.pg.value(
                "SELECT count(*) FROM public.version_repository_migrations WHERE project_id="
                + literal(a.project)
            )
            == "0"
        )
        first = operator(a, s3).prepare()
        count = a.pg.value(
            "SELECT count(*) FROM public.version_repository_migration_objects WHERE project_id="
            + literal(a.project)
        )
        assert operator(a, s3).prepare() == first
        assert (
            a.pg.value(
                "SELECT count(*) FROM public.version_repository_migration_objects WHERE project_id="
                + literal(a.project)
            )
            == count
        )
        assert activate(a) == "1" and activate(a) == "0"
        assert operator(a, s3).prepare()["state"] == "activated"
        assert (
            a.pg.value(
                "SELECT version_root_hash FROM public.projects WHERE id=" + literal(a.project)
            )
            == tree
        )
        assert (
            a.pg.value(
                "SELECT commit_id FROM public.version_commits WHERE project_id="
                + literal(a.project)
            )
            == head
        )
        for oid, (kind, body) in git.objects().items():
            assert migration.decode(operator(a, s3).get(operator(a, s3).key(oid)), oid) == (
                kind,
                body,
            )


def test_native_inventory_missing_source_fences_and_resumes():
    a = fixture(Postgres())
    with owned_s3() as (s3, _):
        oid = "a" * 16
        a.pg.sql(
            "UPDATE public.projects SET version_root_hash="
            + literal(oid)
            + " WHERE id="
            + literal(a.project)
        )
        with pytest.raises(FileNotFoundError):
            operator(a, s3).prepare()
        assert (
            a.pg.value(
                "SELECT authority FROM public.version_repositories WHERE project_id="
                + literal(a.project)
            )
            == "shadow"
        )
        for statement in (
            "UPDATE public.projects SET version_root_hash='changed' WHERE id=" + literal(a.project),
            "DELETE FROM public.projects WHERE id=" + literal(a.project),
            "SELECT public.acquire_project_write_lease("
            + ",".join(map(literal, (a.project, str(uuid.uuid4()), "test", "test")))
            + ")",
        ):
            assert "repository_migration_fenced" in a.pg.sql(statement, check=False).stderr
        assert (
            "migration_organization_incomplete"
            in a.pg.sql(
                "SELECT public.activate_native_repository_migrations(" + literal(a.org) + ")",
                check=False,
            ).stderr
        )
        # Recover actual source bytes, never substitute an empty root in code.
        s3.client.put_object(Bucket=s3.bucket_name, Key=operator(a, s3).key(oid, "mut"), Body=b"{}")
        operator(a, s3).prepare()
        assert activate(a) == "1"


def test_native_inventory_interrupted_upload_is_idempotent(monkeypatch):
    a = fixture(Postgres())
    with owned_s3() as (s3, _):
        upload_raw(a, s3)
        original = migration.Database.sql

        def interrupt(self, sql):
            if "INSERT INTO public.version_repository_migration_objects" in sql:
                raise RuntimeError("injected process interruption after S3 PUT")
            return original(self, sql)

        with monkeypatch.context() as patch:
            patch.setattr(migration.Database, "sql", interrupt)
            with pytest.raises(RuntimeError, match="interruption"):
                operator(a, s3).prepare()
        assert (
            a.pg.value(
                "SELECT count(*) FROM public.version_repository_refs WHERE project_id="
                + literal(a.project)
            )
            == "0"
        )
        operator(a, s3).prepare()
        assert activate(a) == "1"


def test_native_inventory_corrupt_canonical_never_becomes_raw(tmp_path):
    a = fixture(Postgres())
    with owned_s3() as (s3, _):
        oid = "f" * 40
        a.pg.sql(
            "UPDATE public.projects SET version_root_hash="
            + literal(oid)
            + " WHERE id="
            + literal(a.project)
        )
        s3.client.put_object(Bucket=s3.bucket_name, Key=operator(a, s3).key(oid, "mut"), Body=b"{}")
        with pytest.raises((ValueError, migration.zlib.error)):
            operator(a, s3).prepare()
        assert (
            a.pg.value(
                "SELECT count(*) FROM public.version_repository_refs WHERE project_id="
                + literal(a.project)
            )
            == "0"
        )


def test_native_inventory_operator_rpcs_are_not_runtime_enrollment():
    pg = Postgres()
    for role in ("anon", "authenticated", "service_role"):
        result = pg.sql(
            "SET ROLE " + role + "; SELECT public.begin_native_repository_migration('anything');",
            check=False,
        )
        assert result.returncode and "permission denied" in result.stderr


def test_native_inventory_empty_project_persists_implicit_empty_tree():
    a = fixture(Postgres())
    a.pg.sql(
        "UPDATE public.projects SET version_root_hash="
        + literal(migration.EMPTY)
        + " WHERE id="
        + literal(a.project)
    )
    with owned_s3() as (s3, _):
        op = operator(a, s3)
        op.prepare()
        assert op.get(op.key(migration.EMPTY)) is not None
        assert activate(a) == "1"
        assert (
            a.pg.value(
                "SELECT accounted_bytes FROM public.version_repository_billing WHERE project_id="
                + literal(a.project)
            )
            == "0"
        )


def test_native_inventory_tags_branches_and_existing_scope_are_preserved(tmp_path):
    a = fixture(Postgres())
    git = Git.init(tmp_path / "git")
    git.commit({"visible": b"hello"})
    git.run("tag", "-a", "release", "-m", "retained tag")
    tag = git.text("rev-parse", "refs/tags/release")
    with owned_s3() as (s3, _):
        head, _ = upload_git(a, s3, git)
        scope = "scope-" + uuid.uuid4().hex
        a.pg.sql(
            "INSERT INTO public.repository_scopes(id,project_id,name,path,max_mode) VALUES("
            + ",".join(map(literal, (scope, a.project, "Retained scope", "private", "r")))
            + ");"
        )
        before = a.pg.value(
            "SELECT row_to_json(s) FROM public.repository_scopes s WHERE id=" + literal(scope)
        )
        for name, kind, oid in (
            ("refs/heads/feature", "branch", head),
            ("refs/tags/release", "tag", tag),
        ):
            a.pg.sql(
                "INSERT INTO public.version_refs(project_id,ref_name,ref_type,commit_id) VALUES("
                + ",".join(map(literal, (a.project, name, kind, oid)))
                + ");"
            )
        operator(a, s3).prepare()
        activate(a)
        after = a.pg.value(
            "SELECT row_to_json(s) FROM public.repository_scopes s WHERE id=" + literal(scope)
        )
        assert before == after
        assert (
            a.pg.value(
                "SELECT peeled_oid FROM public.version_repository_root_metadata WHERE project_id="
                + literal(a.project)
                + " AND oid="
                + literal(tag)
            )
            == head
        )
        assert (
            "native_scope_not_supported"
            in a.pg.sql(
                "UPDATE public.repository_scopes SET max_mode='rw' WHERE id=" + literal(scope),
                check=False,
            ).stderr
        )


def test_native_inventory_project_drain_and_location_mutation_fences():
    a = fixture(Postgres())
    lease = str(uuid.uuid4())
    a.pg.sql(
        "SELECT public.acquire_project_write_lease("
        + ",".join(map(literal, (a.project, lease, "active", "test")))
        + ");"
    )
    with owned_s3() as (s3, _):
        upload_raw(a, s3)
        with pytest.raises(RuntimeError, match="writers_not_drained"):
            operator(a, s3).prepare()
        a.pg.sql("DELETE FROM public.project_write_leases WHERE id=" + literal(lease))
        operator(a, s3).prepare()
        sql = "SET ROLE service_role; INSERT INTO public.version_object_locations(project_id,object_id,pack_key,offset_bytes,size_bytes) VALUES("
        sql += ",".join(map(literal, (a.project, "a" * 40, "mut/ignored", 0, 10))) + ");"
        assert "repository_migration_fenced" in a.pg.sql(sql, check=False).stderr


def test_native_inventory_organization_activation_is_atomic_and_counts_all_paths():
    pg = Postgres()
    a = fixture(pg)
    b = SimpleNamespace(pg=pg, project="hosting-" + uuid.uuid4().hex, org=a.org)
    pg.sql(
        "BEGIN; INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status,version_root_hash) VALUES("
        + ",".join(
            map(literal, (b.project, "Second project", a.org, a.user, "ready", migration.EMPTY))
        )
        + ");"
        "INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by) VALUES("
        + ",".join(
            map(literal, ("pm-" + uuid.uuid4().hex, a.org, b.project, a.user, "admin", a.user))
        )
        + "); COMMIT;"
    )
    with owned_s3() as (s3, _):
        _, body = upload_raw(a, s3)
        upload_raw(b, s3)
        operator(a, s3).prepare()
        assert (
            "migration_organization_incomplete"
            in pg.sql(
                "SELECT public.activate_native_repository_migrations(" + literal(a.org) + ")",
                check=False,
            ).stderr
        )
        assert (
            pg.value(
                "SELECT authority FROM public.version_repositories WHERE project_id="
                + literal(a.project)
            )
            == "shadow"
        )
        operator(b, s3).prepare()
        assert activate(a) == "2"
        assert (
            int(
                pg.value(
                    "SELECT value FROM public.organization_usage_counters WHERE org_id="
                    + literal(a.org)
                    + " AND metric='storage.logical_bytes'"
                )
            )
            == len(body) * 4
        )


def test_native_inventory_cross_project_location_and_corrupt_destination_fail(tmp_path):
    a = fixture(Postgres())
    git = Git.init(tmp_path / "git")
    git.commit({"file": b"source"})
    with owned_s3() as (s3, _):
        head, _ = upload_git(a, s3, git, "bundle")
        pg = a.pg
        pg.sql(
            "UPDATE public.version_object_locations SET pack_key='version/foreign/object-bundles/anything' WHERE project_id="
            + literal(a.project)
            + " AND object_id="
            + literal(head)
        )
        with pytest.raises(ValueError, match="cross-project"):
            operator(a, s3).prepare()
        # This repair is confined to the disposable test DB; hosted failures
        # require locating the real source rather than fabricating a replacement.
        pg.sql(
            "UPDATE public.version_object_locations SET pack_key="
            + literal(f"mut/{a.project}/object-bundles/{head}.pob")
            + " WHERE project_id="
            + literal(a.project)
            + " AND object_id="
            + literal(head)
        )
        operator(a, s3).prepare()
        s3.client.put_object(
            Bucket=s3.bucket_name, Key=operator(a, s3).key(head), Body=b"corrupted"
        )
        with pytest.raises((ValueError, migration.zlib.error)):
            operator(a, s3).prepare()
        assert (
            pg.value(
                "SELECT count(*) FROM public.version_repository_refs WHERE project_id="
                + literal(a.project)
            )
            == "0"
        )


def test_native_inventory_retention_does_not_disable_gc_for_unmigrated_native_repo(publication):
    assert_unmigrated_gc(publication)


def test_native_inventory_records_actual_mut_schema():
    db = migration.Database(Postgres().url)
    report = {
        "environment": "owned_local_docker",
        "columns": db.rows(
            "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND column_name LIKE 'mut\\_%' ESCAPE '\\' ORDER BY 1,2"
        ),
        "views": db.rows(
            "SELECT table_name FROM information_schema.views WHERE table_schema='public' AND table_name LIKE 'mut\\_%' ESCAPE '\\' ORDER BY 1"
        ),
        "functions": db.rows(
            "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND proname ~ '(^|_)mut_' ORDER BY 1"
        ),
    }
    assert any(
        row["table_name"] == "projects" and row["column_name"] == "mut_root_hash"
        for row in report["columns"]
    )
    Path("/evidence/mut-schema.json").write_text(json.dumps(report, indent=2) + "\n")


def test_native_inventory_raw_history_sequence_and_changed_source():
    a = fixture(Postgres())
    with owned_s3() as (s3, _):
        tree, _ = upload_raw(a, s3)
        for number in range(2):
            a.pg.sql(
                "INSERT INTO public.version_commits(project_id,root_hash,who,message,commit_id) VALUES("
                + ",".join(
                    map(
                        literal,
                        (a.project, tree, "original author", "original message", str(number) * 16),
                    )
                )
                + ");"
            )
        op = operator(a, s3)
        op.prepare()
        refs = json.loads(
            a.pg.value(
                "SELECT refs FROM public.version_repository_migrations WHERE project_id="
                + literal(a.project)
            )
        )
        kind, body = op.read_canonical(refs["refs/heads/main"])
        assert kind == "commit" and b"Imported legacy snapshot: current" in body
        parent = migration.edges(kind, body)[1][0]
        kind, body = op.read_canonical(parent)
        assert (
            b"Imported legacy snapshot: history-" in body and len(migration.edges(kind, body)) == 2
        )
        assert b"original author" in body and b"original message" in body
        # A short legacy key must not silently resolve to different bytes on resume.
        s3.client.put_object(
            Bucket=s3.bucket_name, Key=op.key("1" * 16, "mut"), Body=b"changed after checkpoint"
        )
        with pytest.raises(RuntimeError, match="source_changed_across_attempts"):
            operator(a, s3).prepare()
        assert (
            a.pg.value(
                "SELECT authority FROM public.version_repositories WHERE project_id="
                + literal(a.project)
            )
            == "shadow"
        )


def scoped_fixture(pg, s3, tmp_path):
    a = fixture(pg)
    git = Git.init(tmp_path / 'selected')
    git.commit({'current.txt': b'current'})
    git.commit({'next.txt': b'next'})
    head, _ = upload_git(a, s3, git)
    sibling = 'unselected-' + uuid.uuid4().hex
    pg.sql('BEGIN; INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status,version_root_hash) VALUES('
           + ','.join(map(literal, (sibling, 'Historical repair deferred', a.org, a.user, 'ready', migration.EMPTY))) + ');'
           'INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by) VALUES('
           + ','.join(map(literal, ('member-' + sibling, a.org, sibling, a.user, 'admin', a.user))) + ');'
           'INSERT INTO public.version_commits(project_id,root_hash,who,message,commit_id) VALUES('
           + ','.join(map(literal, (sibling, 'f' * 40, 'fixture', 'missing historical graph', 'e' * 40))) + '); COMMIT;')
    return a, head, sibling


def test_scoped_rollout_preserves_bad_sibling_history_and_is_idempotent(tmp_path):
    pg = Postgres()
    with owned_s3() as (s3, _):
        a, head, sibling = scoped_fixture(pg, s3, tmp_path)
        before = pg.value('SELECT public._native_migration_source(' + literal(sibling) + ');')
        db = migration.Database(pg.url)
        rollout.execute(db, s3.client, s3.bucket_name, [a.project], apply=True)
        assert pg.value('SELECT target_oid FROM public.version_repository_refs WHERE project_id='
                        + literal(a.project) + " AND name=convert_to('refs/heads/main','UTF8');") == head
        assert pg.value('SELECT public._native_migration_source(' + literal(sibling) + ');') == before
        assert pg.value('SELECT count(*) FROM public.version_repositories WHERE project_id=' + literal(sibling)) == '0'
        assert pg.value('SELECT value FROM public.organization_usage_counters WHERE org_id='
                        + literal(a.org) + " AND metric='storage.logical_bytes'") == '11'
        # No new inventory/reconciliation when the same selection is retried.
        captures = pg.value('SELECT count(*) FROM public.version_storage_reconciliations WHERE org_id=' + literal(a.org))
        rollout.execute(db, s3.client, s3.bucket_name, [a.project], apply=True)
        assert pg.value('SELECT count(*) FROM public.version_storage_reconciliations WHERE org_id=' + literal(a.org)) == captures
        assert pg.value("SELECT has_function_privilege('service_role',"
                        "'public.activate_prepared_native_repository_projects(text,text[],uuid)','EXECUTE')") == 'f'


def test_scoped_rollout_rejects_current_snapshot_drift_and_then_resumes(tmp_path):
    pg = Postgres()
    with owned_s3() as (s3, _):
        a, _, sibling = scoped_fixture(pg, s3, tmp_path)
        db = migration.Database(pg.url)
        original_sql = db.sql
        def drift(statement):
            if 'activate_prepared_native_repository_projects' in statement:
                pg.sql('UPDATE public.projects SET version_root_hash=' + literal('d' * 40)
                       + ' WHERE id=' + literal(sibling))
            return original_sql(statement)
        db.sql = drift
        with pytest.raises(RuntimeError, match='storage_reconciliation_snapshot_changed'):
            rollout.execute(db, s3.client, s3.bucket_name, [a.project], apply=True)
        assert pg.value('SELECT authority FROM public.version_repositories WHERE project_id=' + literal(a.project)) == 'shadow'
        assert pg.value('SELECT count(*) FROM public.organization_usage_counters WHERE org_id=' + literal(a.org)) == '0'
        assert pg.value('SELECT count(*) FROM public.version_repository_refs WHERE project_id=' + literal(a.project)) == '0'
        db.sql = original_sql
        pg.sql('UPDATE public.projects SET version_root_hash=' + literal(migration.EMPTY) + ' WHERE id=' + literal(sibling))
        rollout.execute(db, s3.client, s3.bucket_name, [a.project], apply=True)
        assert pg.value('SELECT authority FROM public.version_repositories WHERE project_id=' + literal(a.project)) == 'native'
