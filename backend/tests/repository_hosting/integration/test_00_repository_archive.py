"""Owned Docker upgrade: private data preservation and the final schema contract."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.runner import DataMigrationRunner
from src.version_engine.derived.object_gc import run_git_object_gc
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_native_inventory_migration import (
    activate,
    fixture,
    operator,
    upload_git,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.hosting_migration]
ROOT = Path(__file__).resolve().parents[4]
ARTIFACT = ROOT / "supabase/data_migrations/20261007_repository_recovery_archive"
spec = importlib.util.spec_from_file_location("recovery_archive_artifact", ARTIFACT / "run.py")
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def contract():
    # Roll back the final schema here so the rest of this same disposable stack
    # can continue exercising the historical populated-upgrade fixtures.
    return (ARTIFACT / "contract.pending.sql").read_text().replace("COMMIT;", "ROLLBACK;")


def test_00_empty_contract_has_no_retired_catalog_objects():
    Postgres().sql(contract())


def test_01_private_archive_runner_receipt_and_contract(tmp_path, monkeypatch):
    pg = Postgres()
    a = fixture(pg)
    pg.sql(
        "UPDATE public.projects SET prompt_template="
        + literal(archive.OLD_DEFAULT_PROMPT)
        + " WHERE id="
        + literal(a.project)
    )
    git = Git.init(tmp_path / "source")
    git.commit({"public.txt": b"public"})
    try:
        with owned_s3() as (s3, api):
            upload_git(a, s3, git)
            # A private snapshot exists outside every published Git root.
            blob, blob_bytes = archive.loose("blob", b"private recovery content")
            tree, tree_bytes = archive.loose("tree", b"100644 private.txt\0" + bytes.fromhex(blob))
            for oid, data in [(blob, blob_bytes), (tree, tree_bytes)]:
                s3.client.put_object(
                    Bucket=s3.bucket_name,
                    Key=f"mut/{a.project}/objects/{oid[:2]}/{oid[2:]}",
                    Body=data,
                )
            unknown = f"mut/{a.project}/unindexed-recovery"
            s3.client.put_object(
                Bucket=s3.bucket_name, Key=unknown, Body=b"unclassified original bytes"
            )
            short_key = f"version/{a.project}/objects/ab/0123456789abcd"
            s3.client.put_object(
                Bucket=s3.bucket_name, Key=short_key, Body=b"old truncated-ID object"
            )
            manifest_key = f"shadow-snapshots/{a.project}/old-private/manifest.json"
            manifest_body = json.dumps(
                {"manifest": [], "previews": {"draft": "private text"}}
            ).encode()
            s3.client.put_object(Bucket=s3.bucket_name, Key=manifest_key, Body=manifest_body)
            pg.sql(
                "INSERT INTO public.local_shadow_snapshots(project_id,user_id,tree_hash) VALUES("
                + ",".join(map(literal, (a.project, a.user, tree)))
                + ");"
            )
            operator(a, s3).prepare()
            activate(a)
            blocked = pg.sql(contract(), check=False)
            assert blocked.returncode and "DATA_MIGRATION_REQUIRED" in blocked.stderr
            assert (
                pg.value(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND column_name='mut_root_hash'"
                )
                == "1"
            )
            job = archive.RecoveryArchive(
                archive.Database(pg.url), s3.client, s3.bucket_name, a.project
            )
            assert job.prepare()["state"] == "planned"
            assert pg.value("SELECT count(*) FROM public.version_repository_archives") == "0"
            db = PsqlClient(pg.url)
            env = dict(
                os.environ,
                DATA_MIGRATION_DATABASE_URL=pg.url,
                NATIVE_MIGRATION_WRITERS_DRAINED="yes",
                PATH="/usr/bin:" + os.environ["PATH"],
            )
            runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment=env)
            # A process dies after the first physical deletion. All original
            # bytes are already in the verified archive; resume needs no old graph.
            delete = s3.client.delete_object

            def interrupt_delete(**kwargs):
                delete(**kwargs)
                raise RuntimeError("simulated cleanup interruption")

            with monkeypatch.context() as patch:
                patch.setattr(s3.client, "delete_object", interrupt_delete)
                with pytest.raises(RuntimeError, match="cleanup interruption"):
                    archive.RecoveryArchive(
                        archive.Database(pg.url), s3.client, s3.bucket_name, a.project, apply=True
                    ).prepare()
            assert (
                pg.value(
                    "SELECT state FROM public.version_repository_archives WHERE project_id="
                    + literal(a.project)
                )
                == "verified"
            )
            assert (
                pg.value(
                    "SELECT source_deleted_at IS NULL FROM public.version_repository_archives WHERE project_id="
                    + literal(a.project)
                )
                == "t"
            )
            archived = json.loads(
                pg.value(
                    "SELECT row_to_json(a) FROM public.version_repository_archive_objects a WHERE source_key="
                    + literal(unknown)
                )
            )
            s3.client.put_object(
                Bucket=s3.bucket_name, Key=archived["destination_key"], Body=b"corrupt"
            )
            remaining = job.inventory()
            with pytest.raises(ValueError):
                archive.RecoveryArchive(
                    archive.Database(pg.url), s3.client, s3.bucket_name, a.project, apply=True
                ).prepare()
            assert job.inventory() == remaining
            s3.client.put_object(
                Bucket=s3.bucket_name,
                Key=archived["destination_key"],
                Body=b"unclassified original bytes",
            )
            runner.run(archive.ARTIFACT)
            assert short_key not in job.inventory()
            assert (
                pg.value(
                    "SELECT count(*) FROM public.version_repository_archive_objects WHERE source_key="
                    + literal(short_key)
                )
                == "1"
            )
            receipt = db.receipt(archive.ARTIFACT)
            assert (
                pg.value(
                    "SELECT prompt_template FROM public.projects WHERE id=" + literal(a.project)
                )
                == archive.NATIVE_DEFAULT_PROMPT
            )
            assert (
                json.loads(
                    pg.value(
                        "SELECT source->'project_prompt_template' FROM public.version_repository_archives WHERE project_id="
                        + literal(a.project)
                    )
                )
                == archive.OLD_DEFAULT_PROMPT
            )
            manifest_record = json.loads(
                pg.value(
                    "SELECT row_to_json(a) FROM public.version_repository_archive_objects a WHERE source_key="
                    + literal(manifest_key)
                )
            )
            response = s3.client.get_object(
                Bucket=s3.bucket_name, Key=manifest_record["destination_key"]
            )
            try:
                assert response["Body"].read() == manifest_body
            finally:
                response["Body"].close()
            assert not s3.client.list_objects_v2(
                Bucket=s3.bucket_name, Prefix=f"shadow-snapshots/{a.project}/"
            ).get("Contents")
            runner.run(archive.ARTIFACT)
            assert db.receipt(archive.ARTIFACT) == receipt
            # A cold reader sees canonical private bytes, but no public ref was added.
            assert job.converter.get(job.converter.key(blob)) == blob_bytes
            assert (
                pg.value(
                    "SELECT count(*) FROM public.version_repository_refs WHERE project_id="
                    + literal(a.project)
                    + " AND target_oid="
                    + literal(tree)
                )
                == "0"
            )
            row = json.loads(
                pg.value(
                    "SELECT row_to_json(a) FROM public.version_repository_archive_objects a WHERE source_key="
                    + literal(unknown)
                )
            )
            assert (
                s3.client.get_object(Bucket=s3.bucket_name, Key=row["destination_key"])[
                    "Body"
                ].read()
                == b"unclassified original bytes"
            )
            assert job.inventory() == {}
            custom_prompt = "User-authored project instructions stay unchanged."
            pg.sql(
                "UPDATE public.projects SET prompt_template="
                + literal(custom_prompt)
                + " WHERE id="
                + literal(a.project)
            )
            assert archive.RecoveryArchive(
                archive.Database(pg.url), s3.client, s3.bucket_name, a.project, apply=True
            ).prepare()["source_deleted"]
            assert (
                pg.value(
                    "SELECT prompt_template FROM public.projects WHERE id=" + literal(a.project)
                )
                == custom_prompt
            )
            # Old short IDs and private history must not poison native GC, and
            # the private converted graph remains protected without a Git ref.
            with httpx.Client(timeout=30, trust_env=False) as http:
                sdk = create_client(
                    str(api.client.base_url),
                    api.headers("service_role")["apikey"],
                    ClientOptions(
                        httpx_client=http, auto_refresh_token=False, persist_session=False
                    ),
                )
                gc = run_git_object_gc(
                    VersionRepoManager(s3, SimpleNamespace(client=sdk)).get_gc_repo(a.project),
                    dry_run=False,
                    retention_seconds=0,
                    quarantine_seconds=0,
                )
                assert not gc.errors and not gc.sweep_skipped_for_safety
                assert job.converter.get(job.converter.key(blob)) == blob_bytes
            # A stale legacy writer cannot change an already archived source.
            assert pg.sql(
                "UPDATE public.local_shadow_snapshots SET tree_hash='' WHERE project_id="
                + literal(a.project),
                check=False,
            ).returncode
            pg.sql(contract())
            # Wrong duplicate fields abort without deleting either column.
            pg.sql(
                "ALTER TABLE public.projects DISABLE TRIGGER USER; UPDATE public.projects SET mut_root_hash='mismatch' WHERE id="
                + literal(a.project)
                + "; ALTER TABLE public.projects ENABLE TRIGGER USER;"
            )
            assert "duplicate_version_field_mismatch" in pg.sql(contract(), check=False).stderr
    finally:
        # Deletion is permitted for the owner fixture; no application credentials.
        pg.sql("DELETE FROM public.projects WHERE id=" + literal(a.project) + ";")
        pg.sql("DELETE FROM public.migration_log WHERE name=" + literal(archive.ARTIFACT) + ";")
