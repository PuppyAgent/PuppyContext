"""Formal deletion plus the production cleanup worker over owned PG/S3/Redis.

Bare projects have no search/sandbox/OCR resources. Search is an explicit empty
provider fixture; no external-provider deletion capability is claimed here.
"""

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis

from src.infra.file_processing.ocr.external_cleanup import ExternalIngestCleanup
from src.infra.supabase.client import SupabaseClient
from src.platform.project.deletion_cleanup import (
    ProjectDeletionCleanupWorker,
    ProjectDeletionJobRepository,
    ProjectExternalResourceCleaner,
)
from src.platform.project.write_lease import ProjectWriteLease, active_project_write_lease
from src.platform.upload.tasks.repository import ETLTaskRepositorySupabase
from src.platform.workspace.project_cleanup import ProjectHostCleanupPort
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_bare_repository_application import (
    bare_application as bare_application,
)
from tests.repository_hosting.integration.test_bare_repository_application import create_bare
from tests.repository_hosting.integration.test_docker_application import authorize_git

pytestmark = [pytest.mark.hosting_application, pytest.mark.hosting_s3]


@pytest.mark.asyncio
async def test_bare_delete_waits_for_io_and_settles_usage_after_verified_purge(
    bare_application, tmp_path
):
    app, pg = bare_application, Postgres()
    org, project, remote, secret = create_bare(app, branch="main")
    git = Git.init(tmp_path / "source")
    authorize_git(git, secret)
    oid = git.commit({"body": b"accounted bytes"})
    git.run("push", remote, "main")

    def counter():
        return int(
            pg.value(
                f"SELECT value FROM public.organization_usage_counters WHERE org_id={literal(org)} AND metric='storage.logical_bytes'"
            )
        )

    assert counter() == len(b"accounted bytes")
    assert (
        int(
            pg.value(
                f"SELECT accounted_bytes FROM public.version_repository_billing WHERE project_id={literal(project)}"
            )
        )
        == counter()
    )
    capacity = json.loads(
        pg.value(
            f"SELECT to_jsonb(c) FROM public.version_repository_capacity c WHERE project_id={literal(project)}"
        )
    )
    assert capacity["used_body_bytes"] > 0 and capacity["used_objects"] > 0
    actor = "user:" + pg.value(
        f"SELECT created_by FROM public.projects WHERE id={literal(project)}"
    )
    control = AdmittedRefAuthorityRepository(
        SupabaseClient().client, lease_provider=active_project_write_lease
    )
    pin, io = str(uuid.uuid4()), str(uuid.uuid4())
    # Admit a real storage attempt, then hold its completion across DELETE. The
    # simulated producer performs no PUT; only this test can prove it quiescent.
    async with ProjectWriteLease(project, "deletion-io-acceptance"):
        control.begin(project, actor, pin, 1, {oid: "commit"})
        row = json.loads(
            pg.value(
                f"SELECT jsonb_build_object('object_id',object_id,'object_kind',object_kind,'body_bytes',body_bytes) FROM public.version_repository_object_capacity WHERE project_id={literal(project)} AND object_id={literal(oid)}"
            )
        )
        control.call(
            "reserve_version_object_capacity",
            p_project_id=project,
            p_actor=actor,
            p_pin_id=pin,
            p_objects=[row],
            p_io_id=io,
        )
    accepted = app.api("DELETE", f"/projects/{project}", expected=202)
    job_id = accepted["deletion_job_id"]
    assert git.run("ls-remote", remote, check=False).returncode != 0
    redis = Redis.from_url(os.environ["ETL_REDIS_URL"])

    class EmptySearch:
        async def list_namespaces(self, **_kwargs):
            return SimpleNamespace(namespaces=[])

    try:
        with owned_s3() as (s3, _api):
            worker = ProjectDeletionCleanupWorker(
                ProjectDeletionJobRepository(),
                s3,
                ProjectExternalResourceCleaner(search=EmptySearch()),
                ProjectHostCleanupPort(
                    workspace_base_dir=tmp_path / "workspaces",
                    git_view_cache_dir=tmp_path / "cache",
                ),
                ExternalIngestCleanup(
                    task_source=ETLTaskRepositorySupabase(),
                    redis=redis,
                    providers={},
                    redis_prefix="bare-deletion-tests",
                ),
            )
            waiting = await worker.run_once()
            assert waiting.waiting_for_writers == 1 and waiting.failed == 0
            assert counter() == len(b"accounted bytes")
            assert (
                pg.value(f"SELECT count(*) FROM public.projects WHERE id={literal(project)}") == "1"
            )
            assert (
                control.call(
                    "settle_version_object_capacity_io",
                    p_project_id=project,
                    p_actor=actor,
                    p_pin_id=pin,
                    p_io_id=io,
                )["settled_objects"]
                == 1
            )
            for expected in ("drained", "verification_scheduled", "completed"):
                # Advance only the isolated scheduler fixture, including the
                # persisted quiet-window timestamp; never weaken production timing.
                pg.sql(
                    f"UPDATE public.project_deletion_jobs SET available_at=now()-interval '1 second' WHERE id={literal(job_id)}"
                )
                summary = await worker.run_once()
                assert summary.failed == 0, summary
                assert getattr(summary, expected) == 1, summary
                assert counter() == 0
                if expected != "completed":
                    assert (
                        int(
                            pg.value(
                                f"SELECT used_body_bytes FROM public.version_repository_capacity WHERE project_id={literal(project)}"
                            )
                        )
                        == capacity["used_body_bytes"]
                    )
            assert (
                pg.value(
                    f"SELECT count(*) FROM public.version_repository_capacity WHERE project_id={literal(project)}"
                )
                == "0"
            )
            assert (
                pg.value(
                    f"SELECT used_body_bytes FROM public.version_organization_capacity WHERE org_id={literal(org)}"
                )
                == "0"
            )
            assert (
                pg.value(
                    f"SELECT used_objects FROM public.version_organization_capacity WHERE org_id={literal(org)}"
                )
                == "0"
            )
            assert not ProjectDeletionJobRepository().complete(
                job_id=job_id, worker_id=worker._worker_id
            )
            assert counter() == 0  # duplicate completion never double-refunds
            job = json.loads(
                pg.value(
                    f"SELECT to_jsonb(j) FROM public.project_deletion_jobs j WHERE id={literal(job_id)}"
                )
            )
            assert job["status"] == "completed"
            for prefix in job["object_prefixes"]:
                assert not s3.client.list_objects_v2(Bucket=s3.bucket_name, Prefix=prefix).get(
                    "Contents"
                )
    finally:
        await redis.aclose()
