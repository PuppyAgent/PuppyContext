from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from src.config import settings
from src.infra.file_processing.ocr.external_cleanup import (
    ExternalIngestCleanup,
    ExternalIngestCleanupSnapshot,
)
from src.infra.s3.service import S3Service, get_s3_service_instance
from src.infra.supabase.client import SupabaseClient
from src.infra.turbopuffer.service import TurbopufferSearchService
from src.platform.scope_sandbox.factory import provider_from_settings
from src.platform.scope_sandbox.provider import SandboxState
from src.platform.workspace.project_cleanup import (
    ProjectHostCleanupPort,
)

_logger = logging.getLogger("puppyone.project_deletion")


async def _await_cleanup_boundary(awaitable):
    """Do not abandon a destructive provider operation on cancellation."""

    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.result()
        raise cancelled


@dataclass(frozen=True, slots=True)
class ProjectDeletionCleanupSummary:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    deleted_objects: int = 0
    aborted_multipart_uploads: int = 0
    verification_scheduled: int = 0
    late_object_cycles: int = 0
    drained: int = 0
    waiting_for_writers: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "deleted_objects": self.deleted_objects,
            "aborted_multipart_uploads": self.aborted_multipart_uploads,
            "verification_scheduled": self.verification_scheduled,
            "late_object_cycles": self.late_object_cycles,
            "drained": self.drained,
            "waiting_for_writers": self.waiting_for_writers,
        }


class ProjectDeletionJobRepository:
    def __init__(self, client=None):
        self._client = client or SupabaseClient().get_client()

    def claim(self, *, worker_id: str, limit: int, lease_seconds: int) -> list[dict[str, Any]]:
        response = self._client.rpc(
            "claim_project_deletion_jobs",
            {
                "p_worker_id": worker_id,
                "p_limit": limit,
                "p_lease_seconds": lease_seconds,
            },
        ).execute()
        return [row for row in (response.data or []) if isinstance(row, dict)]

    def complete(self, *, job_id: str, worker_id: str) -> bool:
        response = self._client.rpc(
            "complete_project_deletion_job",
            {"p_job_id": job_id, "p_worker_id": worker_id},
        ).execute()
        return bool(response.data)

    def drain(self, *, job_id: str, worker_id: str) -> dict[str, Any]:
        response = self._client.rpc(
            "drain_project_deletion_job",
            {"p_job_id": job_id, "p_worker_id": worker_id},
        ).execute()
        data = response.data
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise RuntimeError("drain_project_deletion_job returned an invalid response")
        return data

    def persist_external_ingest_snapshot(
        self,
        *,
        job_id: str,
        worker_id: str,
        external_ingest_resources: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._client.rpc(
            "persist_project_deletion_external_ingest_snapshot",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_external_ingest_resources": external_ingest_resources,
            },
        ).execute()
        data = response.data
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise RuntimeError(
                "persist_project_deletion_external_ingest_snapshot returned invalid data"
            )
        return data

    def host_cleanup_tombstones(self) -> list[str]:
        """Return every durable tombstone for this replica's local scrub."""

        response = self._client.rpc(
            "list_project_deletion_host_tombstones",
            {},
        ).execute()
        rows = response.data or []
        if not isinstance(rows, list):
            raise RuntimeError("Host cleanup tombstone RPC returned invalid data")
        return list(
            dict.fromkeys(
                str(row["project_id"])
                for row in rows
                if isinstance(row, dict) and row.get("project_id")
            )
        )

    def schedule_verification(
        self,
        *,
        job_id: str,
        worker_id: str,
        verify_after_seconds: int,
    ) -> bool:
        response = self._client.rpc(
            "schedule_project_deletion_verification",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_verify_after_seconds": verify_after_seconds,
            },
        ).execute()
        return bool(response.data)

    def fail(
        self,
        *,
        job_id: str,
        worker_id: str,
        error: str,
        retry_after_seconds: int,
    ) -> bool:
        response = self._client.rpc(
            "fail_project_deletion_job",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_error": error,
                "p_retry_after_seconds": retry_after_seconds,
            },
        ).execute()
        return bool(response.data)


class ProjectExternalResourceCleaner:
    """Idempotently purge and verify non-S3 provider resources."""

    def __init__(
        self,
        *,
        search: TurbopufferSearchService | None = None,
        sandbox_provider_factory=None,
    ) -> None:
        self._search = search or TurbopufferSearchService()
        self._sandbox_provider_factory = sandbox_provider_factory or (
            lambda name: provider_from_settings(settings, name=name)
        )
        self._sandbox_providers: dict[str, Any] = {}

    async def purge(self, job: dict[str, Any]) -> None:
        search_prefixes, sandboxes = _validated_external_resources(job)
        for prefix in search_prefixes:
            # Deletion mutates the result set. Re-read the first page until it
            # is empty; a cursor from a pre-delete page can skip namespaces.
            previous_ids: tuple[str, ...] | None = None
            while True:
                page = await self._search.list_namespaces(
                    prefix=prefix,
                    page_size=100,
                )
                ids = tuple(str(namespace.id) for namespace in page.namespaces)
                if ids and ids == previous_ids:
                    raise RuntimeError("Search namespace deletion made no observable progress")
                for namespace in page.namespaces:
                    await self._search.delete_namespace(namespace.id)
                if not page.namespaces:
                    break
                previous_ids = ids
        for resource in sandboxes:
            await self._destroy_sandbox(resource)

    async def absent(self, job: dict[str, Any]) -> bool:
        search_prefixes, sandboxes = _validated_external_resources(job)
        for prefix in search_prefixes:
            page = await self._search.list_namespaces(prefix=prefix, page_size=1)
            if page.namespaces:
                return False
        for resource in sandboxes:
            if not await self._sandbox_absent(resource):
                return False
        return True

    def _provider(self, name: str):
        provider = self._sandbox_providers.get(name)
        if provider is None:
            provider = self._sandbox_provider_factory(name)
            self._sandbox_providers[name] = provider
        return provider

    async def _destroy_sandbox(self, resource: dict[str, str]) -> None:
        provider_name = resource["provider"]
        resource_id = resource["resource_id"]
        if provider_name == "docker":
            process = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                resource_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await _await_cleanup_boundary(process.communicate())
            if process.returncode != 0 and b"no such container" not in stderr.lower():
                raise RuntimeError("Unable to remove Project execution container")
            return
        await _await_cleanup_boundary(self._provider(provider_name).destroy(resource_id))

    async def _sandbox_absent(self, resource: dict[str, str]) -> bool:
        provider_name = resource["provider"]
        resource_id = resource["resource_id"]
        if provider_name == "docker":
            process = await asyncio.create_subprocess_exec(
                "docker",
                "inspect",
                resource_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await _await_cleanup_boundary(process.wait()) != 0
        status = await _await_cleanup_boundary(self._provider(provider_name).status(resource_id))
        return status.state is SandboxState.DESTROYED


class ProjectDeletionCleanupWorker:
    def __init__(
        self,
        repository: ProjectDeletionJobRepository,
        s3: S3Service,
        external_resources: ProjectExternalResourceCleaner,
        host_resources: ProjectHostCleanupPort,
        external_ingest: ExternalIngestCleanup,
        *,
        worker_id: str | None = None,
    ):
        self._repository = repository
        self._s3 = s3
        self._external_resources = external_resources
        self._host_resources = host_resources
        self._external_ingest = external_ingest
        self._worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"

    async def run_once(
        self,
        *,
        lease_seconds: int = 3600,
    ) -> ProjectDeletionCleanupSummary:
        # Object deletion can outlive a short database lease.  Claim one job
        # with the full configured lease so no second worker can purge the
        # same Project while this worker is still walking paginated prefixes.
        # Every active replica independently reconciles its non-authoritative
        # local caches from durable deletion tombstones. No replica-local
        # observation is allowed to claim global deletion completion.
        await self._reconcile_host_tombstones()
        jobs = await asyncio.to_thread(
            self._repository.claim,
            worker_id=self._worker_id,
            limit=1,
            lease_seconds=max(30, lease_seconds),
        )
        completed = 0
        failed = 0
        deleted_objects = 0
        aborted_multipart_uploads = 0
        verification_scheduled = 0
        late_object_cycles = 0
        drained = 0
        waiting_for_writers = 0
        for job in jobs:
            job_id = str(job.get("id") or "")
            attempts = max(1, int(job.get("attempts") or 1))
            try:
                prefixes = _validated_project_prefixes(job)
                phase = str(job.get("phase") or "")
                if phase == "drain":
                    outcome = await asyncio.to_thread(
                        self._repository.drain,
                        job_id=job_id,
                        worker_id=self._worker_id,
                    )
                    result = str(outcome.get("outcome") or "")
                    if result == "snapshot_required":
                        ingest_snapshot = await self._external_ingest.snapshot(
                            str(job["project_id"])
                        )
                        if ingest_snapshot.errors:
                            raise RuntimeError("External ingest ownership snapshot is incomplete")
                        persisted = await asyncio.to_thread(
                            self._repository.persist_external_ingest_snapshot,
                            job_id=job_id,
                            worker_id=self._worker_id,
                            external_ingest_resources=ingest_snapshot.to_dict(),
                        )
                        if persisted.get("outcome") not in {"persisted", "replayed"}:
                            raise RuntimeError(
                                "Project deletion resource snapshot was not persisted"
                            )
                        job["external_ingest_resources"] = ingest_snapshot.to_dict()
                        outcome = await asyncio.to_thread(
                            self._repository.drain,
                            job_id=job_id,
                            worker_id=self._worker_id,
                        )
                        result = str(outcome.get("outcome") or "")
                    if result == "drained":
                        await self._purge_non_s3_resources(job)
                        drained += 1
                    elif result == "waiting":
                        waiting_for_writers += 1
                    else:
                        raise RuntimeError(
                            f"Project deletion drain was not acknowledged: {result or 'missing'}"
                        )
                elif phase == "purge":
                    await self._purge_non_s3_resources(job)
                    for prefix in prefixes:
                        aborted_multipart_uploads += await self._abort_prefix_multipart_uploads(
                            prefix
                        )
                        deleted_objects += await self._purge_prefix(prefix)
                    await self._external_resources.purge(job)
                    await self._schedule_verification(job)
                    verification_scheduled += 1
                elif phase == "verify":
                    # S3 is strongly consistent, but an already-admitted Git
                    # request may finish after the first purge.  Seeing any
                    # object restarts the entire quiet verification window.
                    has_late_objects = False
                    has_late_multipart_uploads = False
                    has_late_external_resources = not (await self._external_resources.absent(job))
                    await self._scrub_host_project(str(job["project_id"]))
                    ingest_result = await self._cleanup_external_ingest(job)
                    has_late_ingest_resources = not ingest_result.complete
                    for prefix in prefixes:
                        if await self._prefix_has_objects(prefix):
                            has_late_objects = True
                        if await self._prefix_has_multipart_uploads(prefix):
                            has_late_multipart_uploads = True
                    if (
                        has_late_objects
                        or has_late_multipart_uploads
                        or has_late_external_resources
                        or has_late_ingest_resources
                    ):
                        late_object_cycles += 1
                        for prefix in prefixes:
                            aborted_multipart_uploads += await self._abort_prefix_multipart_uploads(
                                prefix
                            )
                            deleted_objects += await self._purge_prefix(prefix)
                        await self._external_resources.purge(job)
                        await self._schedule_verification(job)
                        verification_scheduled += 1
                    else:
                        acknowledged = await asyncio.to_thread(
                            self._repository.complete,
                            job_id=job_id,
                            worker_id=self._worker_id,
                        )
                        if not acknowledged:
                            raise RuntimeError("deletion job lease was lost before completion")
                        completed += 1
                else:
                    raise RuntimeError(f"Project deletion job has invalid phase {phase!r}")
            except Exception as exc:
                failed += 1
                await asyncio.to_thread(
                    self._repository.fail,
                    job_id=job_id,
                    worker_id=self._worker_id,
                    error=str(exc),
                    retry_after_seconds=min(3600, 15 * (2 ** min(attempts - 1, 8))),
                )
        return ProjectDeletionCleanupSummary(
            claimed=len(jobs),
            completed=completed,
            failed=failed,
            deleted_objects=deleted_objects,
            aborted_multipart_uploads=aborted_multipart_uploads,
            verification_scheduled=verification_scheduled,
            late_object_cycles=late_object_cycles,
            drained=drained,
            waiting_for_writers=waiting_for_writers,
        )

    async def _schedule_verification(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        # The admission transaction persists this safety interval. Workers
        # must not silently substitute a process-local default after restart.
        raw_quiescence = job.get("quiescence_seconds")
        if (
            isinstance(raw_quiescence, bool)
            or not isinstance(raw_quiescence, int)
            or raw_quiescence < 1800
        ):
            raise RuntimeError("Project deletion job is missing its durable quiescence interval")
        verify_after_seconds = raw_quiescence
        acknowledged = await asyncio.to_thread(
            self._repository.schedule_verification,
            job_id=job_id,
            worker_id=self._worker_id,
            verify_after_seconds=verify_after_seconds,
        )
        if not acknowledged:
            raise RuntimeError("deletion job lease was lost before verification scheduling")

    def _persisted_ingest_snapshot(
        self,
        job: dict[str, Any],
    ) -> ExternalIngestCleanupSnapshot:
        value = job.get("external_ingest_resources")
        if not isinstance(value, dict):
            raise RuntimeError("Project deletion job has no durable external ingest snapshot")
        snapshot = ExternalIngestCleanupSnapshot.from_dict(value)
        if snapshot.project_id != str(job.get("project_id") or ""):
            raise RuntimeError("External ingest snapshot ownership mismatch")
        if snapshot.errors:
            raise RuntimeError("External ingest ownership snapshot is incomplete")
        return snapshot

    async def _cleanup_external_ingest(self, job: dict[str, Any]):
        result = await self._external_ingest.cleanup(self._persisted_ingest_snapshot(job))
        return result

    async def _purge_non_s3_resources(self, job: dict[str, Any]) -> None:
        await self._scrub_host_project(str(job["project_id"]))
        ingest_result = await self._cleanup_external_ingest(job)
        if not ingest_result.complete:
            detail = "; ".join(ingest_result.errors[:3]) or ingest_result.state.value
            raise RuntimeError(f"External ingest cleanup is not complete: {detail}")

    async def _scrub_host_project(self, project_id: str) -> None:
        try:
            snapshot = self._host_resources.snapshot(project_id)
            await self._host_resources.delete(snapshot)
            verification = self._host_resources.verify(snapshot)
            if not verification.complete:
                raise RuntimeError("local derived cache remained after scrub")
        except Exception as exc:
            # Host workspaces and Git views are non-authoritative ephemeral
            # caches. Project admission fencing prevents them from ever being
            # served or rebuilt after the tombstone; periodic reconciliation
            # retries local physical scrubbing without blocking global durable
            # resource deletion.
            _logger.warning(
                "project_host_cache_scrub_failed",
                extra={"project_id": project_id, "error_type": type(exc).__name__},
            )

    async def _reconcile_host_tombstones(self) -> None:
        try:
            project_ids = await asyncio.to_thread(self._repository.host_cleanup_tombstones)
        except Exception as exc:
            _logger.warning(
                "project_host_tombstone_scan_failed",
                extra={"error_type": type(exc).__name__},
            )
            return
        for project_id in project_ids:
            await self._scrub_host_project(project_id)

    async def _prefix_has_objects(self, prefix: str) -> bool:
        files, _, _, _ = await self._s3.list_files(prefix=prefix, max_keys=1)
        return bool(files)

    async def _prefix_has_multipart_uploads(self, prefix: str) -> bool:
        uploads, _ = await self._s3.list_multipart_uploads(
            prefix=prefix,
            max_uploads=1,
        )
        return bool(uploads)

    async def _abort_prefix_multipart_uploads(self, prefix: str) -> int:
        aborted = 0
        # Like object deletion, re-read the first page after every batch.  The
        # S3 API's pagination markers refer to a changing set once uploads are
        # aborted, so restarting cannot skip a remaining upload.
        while True:
            uploads, _ = await self._s3.list_multipart_uploads(
                prefix=prefix,
                max_uploads=1000,
            )
            if not uploads:
                return aborted
            for upload in uploads:
                await self._s3.abort_multipart_upload(upload.key, upload.upload_id)
                aborted += 1

    async def _purge_prefix(self, prefix: str) -> int:
        deleted = 0
        # Always re-read the first page after deletion.  S3 continuation tokens
        # are opaque and can skip keys when the preceding page was removed.
        while True:
            files, _, _, _ = await self._s3.list_files(prefix=prefix, max_keys=1000)
            keys = [item.key for item in files]
            if not keys:
                return deleted
            results = await self._s3.delete_files_batch(keys)
            failures = [
                result
                for result in results
                if not result.success and result.message != "File not found"
            ]
            if failures:
                sample = "; ".join(
                    f"{result.key}: {result.message or 'delete failed'}" for result in failures[:3]
                )
                raise RuntimeError(f"unable to purge Project object prefix {prefix}: {sample}")
            deleted += len(keys)


def _validated_project_prefixes(job: dict[str, Any]) -> tuple[str, ...]:
    project_id = str(job.get("project_id") or "")
    raw_prefixes = job.get("object_prefixes")
    raw_principals = job.get("storage_principals")
    requested_by = str(job.get("requested_by") or "")
    if (
        not _STORAGE_SEGMENT.fullmatch(project_id)
        or not isinstance(raw_prefixes, list)
        or not isinstance(raw_principals, list)
        or not requested_by
    ):
        raise RuntimeError("Project deletion job has an invalid object-prefix contract")

    principals = tuple(str(principal) for principal in raw_principals)
    if (
        not principals
        or principals != tuple(sorted(principals))
        or len(set(principals)) != len(principals)
        or requested_by not in principals
        or any(not _STORAGE_SEGMENT.fullmatch(principal) for principal in principals)
    ):
        raise RuntimeError("Project deletion job has invalid storage principals")

    allowed = [
        f"version/{project_id}/",
        # Historical read-only object namespace retained during the immutable
        # object-layout cutover.  It remains Project-owned and must be purged.
        f"projects/{project_id}/",
        f"shadow-snapshots/{project_id}/",
    ]
    for namespace in ("etl_artifacts", "processed", "raw"):
        allowed.extend(f"users/{principal}/{namespace}/{project_id}/" for principal in principals)
    prefixes = tuple(str(prefix) for prefix in raw_prefixes)
    if prefixes != tuple(allowed):
        raise RuntimeError("Project deletion job must contain every exact Project-owned prefix")
    return prefixes


def _validated_external_resources(
    job: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    project_id = str(job.get("project_id") or "")
    if not _STORAGE_SEGMENT.fullmatch(project_id):
        raise RuntimeError("Project deletion job has an invalid Project id")
    raw_prefixes = job.get("search_namespace_prefixes")
    expected = (
        f"project_{project_id}_path_",
        f"project_{project_id}_folder_",
    )
    if not isinstance(raw_prefixes, list) or tuple(raw_prefixes) != expected:
        raise RuntimeError("Project deletion job has invalid search namespace prefixes")
    raw_resources = job.get("sandbox_resources")
    if not isinstance(raw_resources, list):
        raise RuntimeError("Project deletion job has invalid sandbox resources")
    resources: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_resources:
        if not isinstance(raw, dict):
            raise RuntimeError("Project deletion job has invalid sandbox resources")
        kind = str(raw.get("kind") or "")
        provider = str(raw.get("provider") or "")
        resource_id = str(raw.get("resource_id") or "")
        identity = (kind, provider, resource_id)
        if (
            kind not in {"scope", "execution"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", provider)
            or not resource_id
            or len(resource_id) > 512
            or identity in seen
        ):
            raise RuntimeError("Project deletion job has invalid sandbox resources")
        seen.add(identity)
        resources.append({"kind": kind, "provider": provider, "resource_id": resource_id})
    return expected, tuple(resources)


_STORAGE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


async def process_project_deletion_cleanup() -> dict[str, int | str]:
    # Import lazily so ordinary API startup does not construct provider SDKs
    # or Redis connections. Historical providers remain registered forever:
    # old durable handles must stay cancellable after the default changes.
    from src.infra.file_processing.ocr.factory import get_ocr_provider
    from src.platform.upload.config import upload_config
    from src.platform.upload.dependencies import get_etl_arq_pool
    from src.platform.upload.tasks.repository import ETLTaskRepositorySupabase

    external_ingest = ExternalIngestCleanup(
        task_source=ETLTaskRepositorySupabase(),
        redis=await get_etl_arq_pool(),
        redis_prefix=upload_config.etl_redis_prefix,
        providers={name: get_ocr_provider(name) for name in ("mineru", "reducto", "deepseek")},
    )
    worker = ProjectDeletionCleanupWorker(
        ProjectDeletionJobRepository(),
        get_s3_service_instance(),
        ProjectExternalResourceCleaner(),
        ProjectHostCleanupPort(),
        external_ingest,
    )
    summary = await worker.run_once(
        lease_seconds=settings.PROJECT_DELETION_CLEANUP_LEASE_SECONDS,
    )
    return {"status": "ok", **summary.as_dict()}
