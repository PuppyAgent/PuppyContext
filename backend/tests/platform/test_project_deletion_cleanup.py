from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from types import SimpleNamespace

import pytest

from src.platform.project.deletion_cleanup import (
    ProjectDeletionCleanupWorker,
    ProjectExternalResourceCleaner,
    _validated_project_prefixes,
)
from src.platform.scope_sandbox.provider import SandboxInfo, SandboxState

PROJECT_ID = "project-1"
REQUESTED_BY = "00000000-0000-4000-8000-000000000001"
OTHER_PRINCIPAL = "00000000-0000-4000-8000-000000000002"
PRINCIPALS = [REQUESTED_BY, OTHER_PRINCIPAL]
PREFIXES = [
    f"version/{PROJECT_ID}/",
    f"mut/{PROJECT_ID}/",
    f"projects/{PROJECT_ID}/",
    f"shadow-snapshots/{PROJECT_ID}/",
    *[
        f"users/{principal}/{namespace}/{PROJECT_ID}/"
        for namespace in ("etl_artifacts", "processed", "raw")
        for principal in PRINCIPALS
    ],
]


@dataclass
class File:
    key: str


@dataclass
class DeleteResult:
    key: str
    success: bool = True
    message: str = ""


@dataclass
class MultipartUpload:
    key: str
    upload_id: str


class RepositoryStub:
    def __init__(self, jobs):
        self.jobs = jobs
        self._lock = Lock()
        self.claim_calls: list[dict] = []
        self.claimed: list[dict] = []
        self.completed: list[dict] = []
        self.scheduled: list[dict] = []
        self.failed: list[dict] = []
        self.drained: list[dict] = []
        self.persisted: list[dict] = []
        self.drain_outcome = {"outcome": "drained"}

    def claim(self, **kwargs):
        with self._lock:
            self.claim_calls.append(kwargs)
            limit = kwargs["limit"]
            jobs, self.jobs = self.jobs[:limit], self.jobs[limit:]
            self.claimed.extend(
                {"job_id": job["id"], "worker_id": kwargs["worker_id"]}
                for job in jobs
            )
            return jobs

    def complete(self, **kwargs):
        self.completed.append(kwargs)
        return True

    def drain(self, **kwargs):
        self.drained.append(kwargs)
        if isinstance(self.drain_outcome, list):
            return self.drain_outcome.pop(0)
        return self.drain_outcome

    def persist_external_ingest_snapshot(self, **kwargs):
        self.persisted.append(kwargs)
        return {"outcome": "persisted"}

    def host_cleanup_tombstones(self):
        return []

    def schedule_verification(self, **kwargs):
        self.scheduled.append(kwargs)
        return True

    def fail(self, **kwargs):
        self.failed.append(kwargs)
        return True


class S3Stub:
    def __init__(self, keys=(), multipart_uploads=()):
        self.keys = set(keys)
        self.multipart_uploads = set(multipart_uploads)
        self.listed: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.aborted: list[tuple[str, str]] = []

    async def list_files(self, *, prefix, max_keys):
        self.listed.append((prefix, max_keys))
        matches = sorted(key for key in self.keys if key.startswith(prefix))[:max_keys]
        return [File(key) for key in matches], None, None, None

    async def delete_files_batch(self, keys):
        for key in keys:
            self.keys.discard(key)
            self.deleted.append(key)
        return [DeleteResult(key) for key in keys]

    async def list_multipart_uploads(self, *, prefix, max_uploads):
        matches = sorted(
            item for item in self.multipart_uploads if item[0].startswith(prefix)
        )[:max_uploads]
        return [MultipartUpload(key, upload_id) for key, upload_id in matches], None

    async def abort_multipart_upload(self, key, upload_id):
        self.multipart_uploads.discard((key, upload_id))
        self.aborted.append((key, upload_id))


class ExternalStub:
    def __init__(self, *, absent: bool = True):
        self.is_absent = absent
        self.purged: list[str] = []

    async def purge(self, job):
        self.purged.append(job["id"])
        self.is_absent = True

    async def absent(self, _job):
        return self.is_absent


@dataclass(frozen=True)
class HostSnapshot:
    project_id: str

    def to_dict(self):
        return {"project_id": self.project_id}


class HostStub:
    def __init__(self, *, complete: bool = True):
        self.complete = complete
        self.deleted: list[str] = []

    def snapshot(self, project_id):
        return HostSnapshot(project_id)

    def restore(self, value):
        return HostSnapshot(value["project_id"])

    async def delete(self, snapshot):
        self.deleted.append(snapshot.project_id)
        self.complete = True

    def verify(self, _snapshot):
        return SimpleNamespace(complete=self.complete)


class ExternalIngestStub:
    def __init__(self, *, complete: bool = True):
        self.complete = complete
        self.cleaned: list[str] = []

    async def snapshot(self, project_id):
        from src.infra.file_processing.ocr.external_cleanup import ExternalIngestCleanupSnapshot

        return ExternalIngestCleanupSnapshot(project_id=project_id)

    async def cleanup(self, snapshot):
        self.cleaned.append(snapshot.project_id)
        return SimpleNamespace(
            complete=self.complete,
            errors=(),
            state=SimpleNamespace(value="complete" if self.complete else "pending"),
        )


def _worker(repository, s3, external, *, worker_id="worker-1", host=None, ingest=None):
    return ProjectDeletionCleanupWorker(
        repository,
        s3,
        external,
        host or HostStub(),
        ingest or ExternalIngestStub(),
        worker_id=worker_id,
    )


def _job(*, phase: str, prefixes=PREFIXES, principals=PRINCIPALS):
    return {
        "id": "job-1",
        "project_id": PROJECT_ID,
        "requested_by": REQUESTED_BY,
        "storage_principals": list(principals),
        "object_prefixes": list(prefixes),
        "phase": phase,
        "attempts": 1,
        "quiescence_seconds": 1800,
        "search_namespace_prefixes": [
            f"project_{PROJECT_ID}_path_",
            f"project_{PROJECT_ID}_folder_",
        ],
        "sandbox_resources": [],
        "external_ingest_resources": {
            "project_id": PROJECT_ID,
            "provider_handles": [],
            "redis_keys": [],
            "cache_task_ids": [],
            "etl_task_ids": [],
            "arq_job_ids": [],
            "errors": [],
        },
    }


@pytest.mark.asyncio
async def test_first_phase_purges_every_owned_namespace_then_waits_for_verification():
    repository = RepositoryStub([_job(phase="purge")])
    s3 = S3Stub(
        {
            f"version/{PROJECT_ID}/objects/aa/object",
            f"mut/{PROJECT_ID}/objects/bb/legacy",
            f"projects/{PROJECT_ID}/uploads/user/staging",
            f"shadow-snapshots/{PROJECT_ID}/snapshot/manifest.json",
            f"users/{REQUESTED_BY}/raw/{PROJECT_ID}/raw.pdf",
            f"users/{OTHER_PRINCIPAL}/etl_artifacts/{PROJECT_ID}/task/mineru.md",
            f"users/{OTHER_PRINCIPAL}/processed/{PROJECT_ID}/task.json",
        },
        {
            (f"projects/{PROJECT_ID}/uploads/{REQUESTED_BY}/large.bin", "upload-1"),
            (f"users/{OTHER_PRINCIPAL}/raw/{PROJECT_ID}/legacy.bin", "upload-2"),
        },
    )
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert not s3.keys
    assert not s3.multipart_uploads
    assert summary.deleted_objects == 7
    assert summary.aborted_multipart_uploads == 2
    assert summary.completed == 0
    assert summary.verification_scheduled == 1
    assert repository.completed == []
    assert repository.scheduled == [
        {
            "job_id": "job-1",
            "worker_id": "worker-1",
            "verify_after_seconds": 1800,
        }
    ]


@pytest.mark.asyncio
async def test_drain_phase_reschedules_while_an_admitted_writer_is_active():
    repository = RepositoryStub([_job(phase="drain")])
    repository.drain_outcome = {"outcome": "waiting", "active_leases": 1}
    worker = _worker(repository, S3Stub(), ExternalStub())

    summary = await worker.run_once()

    assert summary.waiting_for_writers == 1
    assert summary.drained == 0
    assert repository.drained == [
        {"job_id": "job-1", "worker_id": "worker-1"}
    ]


@pytest.mark.asyncio
async def test_drain_snapshots_before_relational_delete_then_purges_host_and_ingest():
    job = _job(phase="drain")
    job.pop("external_ingest_resources")
    repository = RepositoryStub([job])
    repository.drain_outcome = [
        {"outcome": "snapshot_required"},
        {"outcome": "drained"},
    ]
    host = HostStub()
    ingest = ExternalIngestStub()
    worker = _worker(
        repository,
        S3Stub(),
        ExternalStub(),
        host=host,
        ingest=ingest,
    )

    summary = await worker.run_once()

    assert summary.drained == 1
    assert len(repository.drained) == 2
    assert len(repository.persisted) == 1
    assert "host_resources" not in repository.persisted[0]
    assert host.deleted == [PROJECT_ID]
    assert ingest.cleaned == [PROJECT_ID]


@pytest.mark.asyncio
async def test_verify_phase_completes_only_after_all_prefixes_remain_empty():
    repository = RepositoryStub([_job(phase="verify")])
    s3 = S3Stub()
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert summary.completed == 1
    assert summary.verification_scheduled == 0
    assert repository.completed == [{"job_id": "job-1", "worker_id": "worker-1"}]


@pytest.mark.asyncio
async def test_late_inflight_git_object_restarts_a_full_verification_window():
    late_key = f"version/{PROJECT_ID}/object-bundles/late-pack"
    repository = RepositoryStub([_job(phase="verify")])
    s3 = S3Stub({late_key})
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert late_key in s3.deleted
    assert summary.completed == 0
    assert summary.late_object_cycles == 1
    assert summary.verification_scheduled == 1
    assert repository.completed == []


@pytest.mark.asyncio
async def test_late_multipart_upload_is_aborted_and_restarts_verification_window():
    upload = (
        f"projects/{PROJECT_ID}/uploads/{REQUESTED_BY}/late-large.bin",
        "upload-late",
    )
    repository = RepositoryStub([_job(phase="verify")])
    s3 = S3Stub(multipart_uploads={upload})
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert upload in s3.aborted
    assert not s3.multipart_uploads
    assert summary.aborted_multipart_uploads == 1
    assert summary.completed == 0
    assert summary.late_object_cycles == 1
    assert summary.verification_scheduled == 1
    assert repository.completed == []


@pytest.mark.asyncio
async def test_late_search_or_sandbox_resource_restarts_verification_window():
    repository = RepositoryStub([_job(phase="verify")])
    external = ExternalStub(absent=False)
    worker = _worker(repository, S3Stub(), external)

    summary = await worker.run_once()

    assert summary.completed == 0
    assert summary.late_object_cycles == 1
    assert summary.verification_scheduled == 1
    assert external.purged == ["job-1"]


@pytest.mark.asyncio
async def test_late_host_cache_is_scrubbed_but_does_not_claim_global_authority():
    repository = RepositoryStub([_job(phase="verify")])
    host = HostStub(complete=False)
    worker = _worker(
        repository,
        S3Stub(),
        ExternalStub(),
        host=host,
    )

    summary = await worker.run_once()

    assert summary.completed == 1
    assert summary.verification_scheduled == 0
    assert host.deleted == [PROJECT_ID]
    assert repository.completed == [
        {"job_id": "job-1", "worker_id": "worker-1"}
    ]


@pytest.mark.asyncio
async def test_missing_durable_quiescence_interval_fails_closed():
    job = _job(phase="purge")
    job.pop("quiescence_seconds")
    repository = RepositoryStub([job])
    worker = _worker(repository, S3Stub(), ExternalStub())

    summary = await worker.run_once()

    assert summary.failed == 1
    assert repository.scheduled == []
    assert "durable quiescence" in repository.failed[0]["error"]


@pytest.mark.asyncio
async def test_every_replica_periodically_scrubs_all_durable_tombstones():
    repository = RepositoryStub([])
    repository.host_cleanup_tombstones = lambda: [PROJECT_ID]  # type: ignore[method-assign]
    host = HostStub()
    worker = _worker(
        repository,
        S3Stub(),
        ExternalStub(),
        host=host,
    )

    summary = await worker.run_once()

    assert summary.claimed == 0
    assert host.deleted == [PROJECT_ID]


@pytest.mark.asyncio
async def test_prefix_escape_fails_closed_without_touching_storage():
    repository = RepositoryStub([_job(phase="purge", prefixes=["version/other/"])])
    s3 = S3Stub({"version/other/do-not-delete"})
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert summary.failed == 1
    assert s3.deleted == []
    assert repository.failed[0]["job_id"] == "job-1"
    assert "every exact Project-owned prefix" in repository.failed[0]["error"]


@pytest.mark.asyncio
async def test_missing_owned_prefix_fails_closed_instead_of_leaking_objects():
    repository = RepositoryStub([_job(phase="purge", prefixes=PREFIXES[:2])])
    s3 = S3Stub({f"projects/{PROJECT_ID}/uploads/left-behind"})
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert summary.failed == 1
    assert not s3.deleted
    assert s3.keys == {f"projects/{PROJECT_ID}/uploads/left-behind"}


@pytest.mark.asyncio
async def test_missing_shadow_or_user_namespace_fails_closed():
    repository = RepositoryStub([_job(phase="purge", prefixes=PREFIXES[:3])])
    s3 = S3Stub({f"shadow-snapshots/{PROJECT_ID}/snapshot/manifest.json"})
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert summary.failed == 1
    assert not s3.deleted


@pytest.mark.asyncio
async def test_unpersisted_or_path_escaping_storage_principal_fails_closed():
    repository = RepositoryStub(
        [_job(phase="purge", principals=[REQUESTED_BY, "../another-project"])]
    )
    s3 = S3Stub({"users/another-project/raw/project-1/do-not-delete"})
    worker = _worker(repository, s3, ExternalStub())

    summary = await worker.run_once()

    assert summary.failed == 1
    assert not s3.deleted
    assert "invalid storage principals" in repository.failed[0]["error"]


@pytest.mark.asyncio
async def test_two_workers_claim_one_long_running_job_at_most_once():
    repository = RepositoryStub([_job(phase="verify")])
    s3 = S3Stub()
    first = _worker(repository, s3, ExternalStub(), worker_id="worker-1")
    second = _worker(repository, s3, ExternalStub(), worker_id="worker-2")

    summaries = await asyncio.gather(first.run_once(), second.run_once())

    assert sum(summary.claimed for summary in summaries) == 1
    assert sum(summary.completed for summary in summaries) == 1
    assert len(repository.claimed) == 1
    assert repository.claimed[0]["job_id"] == "job-1"
    assert repository.claimed[0]["worker_id"] in {"worker-1", "worker-2"}
    assert len(repository.claim_calls) == 2
    assert all(call["limit"] == 1 for call in repository.claim_calls)
    assert all(call["lease_seconds"] == 3600 for call in repository.claim_calls)


def test_project_prefix_contract_is_exact_and_includes_every_owned_namespace():
    assert _validated_project_prefixes(_job(phase="purge")) == tuple(PREFIXES)


@pytest.mark.asyncio
async def test_external_cleaner_deletes_search_namespaces_and_sandbox_handles():
    class Search:
        def __init__(self):
            self.namespaces = {
                f"project_{PROJECT_ID}_path_docs",
                f"project_{PROJECT_ID}_folder_root",
                "project_other_path_keep",
            }

        async def list_namespaces(self, *, prefix, **_kwargs):
            return SimpleNamespace(
                namespaces=[
                    SimpleNamespace(id=value)
                    for value in sorted(self.namespaces)
                    if value.startswith(prefix)
                ],
                next_cursor=None,
            )

        async def delete_namespace(self, namespace):
            self.namespaces.discard(namespace)

    class Provider:
        def __init__(self):
            self.destroyed: set[str] = set()

        async def destroy(self, resource_id):
            self.destroyed.add(resource_id)

        async def status(self, resource_id):
            state = (
                SandboxState.DESTROYED
                if resource_id in self.destroyed
                else SandboxState.RUNNING
            )
            return SandboxInfo(resource_id, state)

    search = Search()
    provider = Provider()
    cleaner = ProjectExternalResourceCleaner(
        search=search,  # type: ignore[arg-type]
        sandbox_provider_factory=lambda _name: provider,
    )
    job = _job(phase="purge")
    job["sandbox_resources"] = [
        {"kind": "scope", "provider": "e2b", "resource_id": "sandbox-1"}
    ]

    await cleaner.purge(job)

    assert search.namespaces == {"project_other_path_keep"}
    assert provider.destroyed == {"sandbox-1"}
    assert await cleaner.absent(job)


@pytest.mark.asyncio
async def test_docker_cleanup_removes_container_instead_of_only_stopping(
    monkeypatch,
):
    calls: list[tuple[str, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create_process(*args, **_kwargs):
        calls.append(tuple(args))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    cleaner = ProjectExternalResourceCleaner(
        search=SimpleNamespace(),  # type: ignore[arg-type]
    )

    await cleaner._destroy_sandbox(  # noqa: SLF001 - exact provider command contract
        {"kind": "execution", "provider": "docker", "resource_id": "container-1"}
    )

    assert calls == [("docker", "rm", "-f", "container-1")]
