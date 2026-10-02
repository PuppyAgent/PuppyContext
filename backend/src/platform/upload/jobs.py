"""
ETL ARQ Jobs

OCR job enqueues postprocess job on success.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.infra.supabase.client import SupabaseClient
from src.infra.file_processing.config import etl_config
from src.platform.upload.config import upload_config
from src.infra.file_processing.exceptions import ETLTransformationError
from src.infra.file_processing.ocr.base import (
    OCRExternalJob,
    OCRExternalJobCompletion,
    OCRProvider,
    OCRProviderError,
)
from src.infra.file_processing.ocr.lifecycle import run_ocr_lifecycle_under_project_lease
from src.infra.file_processing.rules.engine import RuleEngine
from src.infra.file_processing.rules.repository_supabase import RuleRepositorySupabase
from src.platform.upload.state.models import ETLPhase, ETLRuntimeState
from src.platform.upload.state.repository import ETLStateRepositoryRedis
from src.platform.upload.tasks.models import ETLTaskResult, ETLTaskStatus
from src.platform.project.write_lease import ProjectWriteLease
from src.version_engine.adapters.product.operation_adapter import BlobRef

logger = logging.getLogger(__name__)


def _version_object_key(project_id: str, blob_hash: str) -> str:
    """Mirror of ``S3StorageBackend._key_for`` so we can pre-stage blobs
    at the exact key the version object store will look at.

    The 2-char shard prefix is intentional and must stay in sync with
    ``version_engine/server/backends/s3_storage.py`` (search for
    ``_HASH_PREFIX_LEN``). Drift here would silently break
    pre-staging — the blob would land at a key the version object store cannot see.
    """
    return f"version/{project_id}/objects/{blob_hash[:2]}/{blob_hash[2:]}"


async def stage_blob_from_s3(
    s3,
    *,
    project_id: str,
    src_key: str,
) -> BlobRef:
    """Stage an uploaded file as a Git-compatible blob object.

    The source S3 object is raw user bytes. PuppyOne's object store now expects
    Git loose-object bytes, so this worker must write
    ``zlib(b"blob <size>\\0" + content)`` under the Git blob SHA-1. A direct S3
    ``CopyObject`` would store invalid object bytes for the Git kernel.
    """
    size = await _head_object_size(s3, src_key)
    blob_hash, loose_bytes = await _encode_s3_object_as_git_blob_loose(
        s3,
        src_key=src_key,
        size=size,
    )

    dst_key = _version_object_key(project_id, blob_hash)
    # Existence-only dedup would be a footgun: an older code path
    # (server-side ``copy_object`` from the upload staging key, see
    # ``infra/s3/service.py::copy_object`` docstring) wrote the **raw**
    # uploaded bytes under exactly this dst_key, before the object store
    # switched to Git loose-object framing. A bare ``object_exists``
    # check would skip the re-upload and trust those legacy raw bytes,
    # which then explodes downstream with
    # ``invalid git loose object … incorrect header check`` when the
    # version engine tries to ``zlib.decompress`` them on read.
    #
    # The Git loose-object format is content-addressed AND
    # deterministic: same blob hash + same encoder = identical
    # zlib stream of identical byte length. So a stored object whose
    # ContentLength matches ``len(loose_bytes)`` is, in practice, the
    # same bytes (size collision with a *different* byte string at the
    # same SHA-1 of the *raw* content is astronomically unlikely on a
    # per-project basis). Anything else is stale/raw and must be
    # overwritten.
    stale_existing = False
    try:
        existing_meta = await s3.get_file_metadata(dst_key)
    except Exception:
        existing_meta = None
    if existing_meta is not None:
        if existing_meta.size == len(loose_bytes):
            logger.info(
                f"stage_blob_from_s3: blob {blob_hash[:12]} already at "
                f"{dst_key} ({existing_meta.size} bytes), skipping upload",
            )
            return BlobRef(hash=blob_hash, size=size)
        stale_existing = True
        logger.warning(
            f"stage_blob_from_s3: blob {blob_hash[:12]} at {dst_key} has "
            f"unexpected size {existing_meta.size} (expected "
            f"{len(loose_bytes)} for the loose Git format); overwriting "
            f"to recover from legacy raw-bytes finalize",
        )

    await s3.upload_file(
        dst_key,
        loose_bytes,
        content_type="application/octet-stream",
    )
    if stale_existing:
        logger.info(
            f"stage_blob_from_s3: overwrote stale {blob_hash[:12]} at {dst_key}",
        )
    return BlobRef(hash=blob_hash, size=size)


async def _encode_s3_object_as_git_blob_loose(
    s3,
    *,
    src_key: str,
    size: int,
) -> tuple[str, bytes]:
    """Stream raw S3 bytes into Git loose-object bytes."""

    sha1 = hashlib.sha1()
    compressor = zlib.compressobj()
    compressed: list[bytes] = []

    def feed(chunk: bytes) -> None:
        sha1.update(chunk)
        part = compressor.compress(chunk)
        if part:
            compressed.append(part)

    feed(f"blob {size}".encode("ascii") + b"\x00")
    actual_size = 0
    async for chunk in s3.download_file_stream(src_key, chunk_size=64 * 1024):
        actual_size += len(chunk)
        feed(chunk)

    if actual_size != size:
        raise ETLTransformationError(
            f"S3 object size changed while staging {src_key}: "
            f"expected {size}, got {actual_size}"
        )

    tail = compressor.flush()
    if tail:
        compressed.append(tail)
    return sha1.hexdigest(), b"".join(compressed)


async def _head_object_size(s3, key: str) -> int:
    """Return the byte length of an S3 object via HEAD.

    Helper kept private to this module — exposed publicly we'd
    want a more general "object metadata" facade. For our one
    use case (size for audit when we trust caller-supplied
    hash) this is enough.
    """
    # ``S3Service`` doesn't yet expose a typed head method; use
    # the underlying boto client. The thread-pool wrapper is the
    # same pattern other methods on the service use.
    def _head():
        return s3.client.head_object(Bucket=s3.bucket_name, Key=key)

    response = await asyncio.to_thread(_head)
    return int(response["ContentLength"])


def _creator_id(task) -> str:
    """Creator ID for S3 paths (created_by or project_id fallback for legacy)."""
    return task.created_by or task.project_id or "unknown"


def _reload_task_before_project_storage_write(repo, task_id: str | int, task):
    """Revalidate the durable Project-owned task immediately before S3 PUT.

    Project deletion cascades ``uploads`` in the same transaction that emits
    the durable object-cleanup job.  OCR/LLM calls can outlive the cleanup
    quiescence window, so a worker must not trust the task object it loaded
    before an external wait.  Missing/cancelled/rebound rows close the write
    fence; repository failures propagate so ARQ retries without writing.
    """

    durable = repo.get_task(task_id)
    if durable is None:
        logger.info(
            "project storage write skipped because durable task disappeared: %s",
            task_id,
        )
        return None
    if durable.status == ETLTaskStatus.CANCELLED:
        logger.info(
            "project storage write skipped because durable task was cancelled: %s",
            task_id,
        )
        return None
    if (
        durable.project_id != task.project_id
        or _creator_id(durable) != _creator_id(task)
    ):
        logger.warning(
            "project storage write skipped because durable task ownership changed: %s",
            task_id,
        )
        return None
    return durable


def _artifact_markdown_key(task_id: str | int, creator_id: str, project_id: str) -> str:
    # Avoid using filename here (can have special chars). Use deterministic keys.
    return f"users/{creator_id}/etl_artifacts/{project_id}/{task_id}/mineru.md"


def _output_json_key(task_id: str | int, creator_id: str, project_id: str) -> str:
    return f"users/{creator_id}/processed/{project_id}/{task_id}.json"


def _chunk_text(text: str, chunk_size: int, max_chunks: int) -> list[str]:
    if chunk_size <= 0:
        return [text]
    chunks: list[str] = []
    for i in range(0, len(text), chunk_size):
        if len(chunks) >= max_chunks:
            break
        chunks.append(text[i : i + chunk_size])
    return chunks


async def etl_ocr_job(ctx: dict, task_id: str | int) -> dict:
    """
    OCR stage: Parse document -> upload markdown artifact -> enqueue postprocess job.

    Supports multiple OCR providers (MineRU, Reducto, etc.) via pluggable OCRProvider interface.
    """
    repo = ctx["task_repository"]
    s3 = ctx["s3_service"]
    ocr_provider: OCRProvider = ctx["ocr_provider"]
    state_repo: ETLStateRepositoryRedis = ctx["state_repo"]
    queue_name: str = ctx["arq_queue_name"]

    task = repo.get_task(task_id)
    if not task:
        logger.warning(f"etl_ocr_job: task not found: {task_id}")
        return {"ok": False, "error": "task_not_found"}

    # Respect DB-level cancellation as a safety net (e.g. Redis state expired)
    if task.status == ETLTaskStatus.CANCELLED:
        logger.info(f"etl_ocr_job: task cancelled in DB, skip: {task_id}")
        return {"ok": True, "skipped": "cancelled"}

    # Runtime state init or load
    state = await state_repo.get(task_id)
    if state is None:
        state = ETLRuntimeState(
            task_id=task_id,
            user_id=_creator_id(task),
            project_id=task.project_id,
            filename=task.filename,
            rule_id=task.rule_id,
        )

    if state.status == ETLTaskStatus.CANCELLED:
        logger.info(f"etl_ocr_job: task cancelled, skip: {task_id}")
        return {"ok": True, "skipped": "cancelled"}

    state.phase = ETLPhase.OCR
    state.status = ETLTaskStatus.MINERU_PARSING
    state.progress = max(state.progress, 10)
    state.attempt_ocr += 1
    state.touch()
    await state_repo.set(state)

    started_at = time.time()
    try:
        # Resolve source S3 key
        if "s3_key" in task.metadata:
            source_key = task.metadata["s3_key"]
        else:
            source_key = f"users/{_creator_id(task)}/raw/{task.project_id}/{task.filename}"

        presigned_url = await s3.generate_presigned_download_url(
            source_key, expires_in=3600
        )

        async def persist_provider_handle(
            provider_job: OCRExternalJob,
            *,
            terminal: bool,
        ) -> None:
            metadata = {
                **provider_job.persistence_metadata(),
                "provider_task_terminal": terminal,
            }
            state.provider_name = provider_job.provider
            state.provider_task_id = provider_job.task_id
            state.provider_task_external = provider_job.external
            state.provider_task_terminal = terminal
            state.metadata.update(metadata)
            state.touch()
            # Do not begin the provider wait (or report it terminal) until both
            # durable discovery sources contain the exact same handle.
            await state_repo.set(state)
            task.metadata.update(metadata)
            persisted = await asyncio.to_thread(repo.update_task, task)
            if persisted is None:
                raise RuntimeError(
                    "Unable to persist OCR provider lifecycle in SQL"
                )

        async def on_created(provider_job: OCRExternalJob) -> None:
            await persist_provider_handle(
                provider_job,
                terminal=not provider_job.external,
            )

        async def on_terminal(completion: OCRExternalJobCompletion) -> None:
            await persist_provider_handle(completion.job, terminal=True)

        # The renewable Project lease spans provider creation, durable handle
        # persistence, the external wait, and local materialization. Deletion
        # can therefore snapshot only after this lifecycle is terminal.
        parsed = await run_ocr_lifecycle_under_project_lease(
            lease_factory=ctx.get("project_write_lease_factory", ProjectWriteLease),
            project_id=task.project_id,
            provider=ocr_provider,
            file_url=presigned_url,
            data_id=str(task_id),
            on_created=on_created,
            on_terminal=on_terminal,
        )

        if state.provider_task_id is None:
            # Inline providers expose no remote cancellable job, but persisting
            # their terminal identity makes historical cleanup classification
            # explicit instead of relying on a provider-name default.
            await persist_provider_handle(
                OCRExternalJob(
                    provider=ocr_provider.name,
                    task_id=parsed.task_id,
                    external=False,
                ),
                terminal=True,
            )

        # If user cancelled while we were waiting on provider, honor cancellation and avoid overwriting terminal state.
        latest = await state_repo.get(task_id)
        if (
            latest and latest.status == ETLTaskStatus.CANCELLED
        ) or task.status == ETLTaskStatus.CANCELLED:
            logger.info(f"etl_ocr_job: cancelled during provider wait, skip: {task_id}")
            return {"ok": True, "skipped": "cancelled"}

        state.provider_task_id = parsed.task_id
        state.progress = max(state.progress, 40)
        await state_repo.set(state)

        # Upload markdown artifact to S3
        if _reload_task_before_project_storage_write(repo, task_id, task) is None:
            return {"ok": True, "skipped": "task_not_live"}
        md_key = _artifact_markdown_key(task_id, _creator_id(task), task.project_id)
        await s3.upload_file(
            key=md_key,
            content=parsed.markdown_content.encode("utf-8"),
            content_type="text/markdown",
            metadata={"task_id": str(task_id), "provider_task_id": parsed.task_id},
        )
        state.artifact_mineru_markdown_key = md_key
        state.progress = max(state.progress, 55)
        await state_repo.set(state)

        latest = await state_repo.get(task_id)
        if (
            latest and latest.status == ETLTaskStatus.CANCELLED
        ) or task.status == ETLTaskStatus.CANCELLED:
            logger.info(
                f"etl_ocr_job: cancelled before enqueue postprocess, skip: {task_id}"
            )
            return {"ok": True, "skipped": "cancelled"}

        # Enqueue postprocess
        job = await ctx["redis"].enqueue_job(
            "etl_postprocess_job", task_id, _queue_name=queue_name
        )
        state.arq_job_id_postprocess = job.job_id
        state.phase = ETLPhase.POSTPROCESS
        state.status = ETLTaskStatus.LLM_PROCESSING
        state.progress = max(state.progress, 60)
        await state_repo.set(state)

        elapsed = time.time() - started_at
        return {"ok": True, "stage": "ocr", "seconds": elapsed, "markdown_key": md_key}

    except asyncio.CancelledError:
        # ARQ enforces WorkerSettings.job_timeout via asyncio.wait_for which cancels the job task.
        # On Python 3.12, asyncio.CancelledError inherits BaseException, so a plain `except Exception`
        # won't run and the runtime state would stay stuck at MINERU_PARSING.
        state.status = ETLTaskStatus.FAILED
        state.error_stage = "timeout"
        state.error_message = f"ETL OCR job timed out (>{upload_config.etl_task_timeout}s)"
        state.progress = 0
        await state_repo.set_terminal(state)

        task.status = ETLTaskStatus.FAILED
        task.error = state.error_message
        task.metadata.update(
            {"error_stage": "timeout", "provider_task_id": state.provider_task_id}
        )
        repo.update_task(task)
        logger.error(f"etl_ocr_job timeout task_id={task_id}")
        return {"ok": False, "stage": "ocr", "error": state.error_message}

    except OCRProviderError as e:
        # Handle OCR provider-specific errors
        state.status = ETLTaskStatus.FAILED
        state.error_stage = f"ocr_{e.provider}"
        state.error_message = str(e)
        state.progress = 0
        await state_repo.set_terminal(state)

        task.status = ETLTaskStatus.FAILED
        task.error = str(e)
        task.metadata.update(
            {
                "error_stage": f"ocr_{e.provider}",
                "provider_task_id": state.provider_task_id,
            }
        )
        repo.update_task(task)
        logger.error(f"etl_ocr_job failed task_id={task_id}: {e}")
        return {"ok": False, "stage": "ocr", "error": str(e)}

    except Exception as e:
        # Handle unexpected errors
        state.status = ETLTaskStatus.FAILED
        state.error_stage = f"ocr_{ocr_provider.name}"
        state.error_message = str(e)
        state.progress = 0
        await state_repo.set_terminal(state)

        task.status = ETLTaskStatus.FAILED
        task.error = str(e)
        task.metadata.update(
            {
                "error_stage": f"ocr_{ocr_provider.name}",
                "provider_task_id": state.provider_task_id,
            }
        )
        repo.update_task(task)
        logger.error(f"etl_ocr_job failed task_id={task_id}: {e}", exc_info=True)
        return {"ok": False, "stage": "ocr", "error": str(e)}


async def finalize_upload_to_version(
    *,
    task_id: str | int,
    repo,
    s3,
    state_repo: ETLStateRepositoryRedis,
) -> dict:
    """
    Core helper: pull a completed multipart upload from S3 and write it
    into Version Engine, marking the task COMPLETED (or FAILED).

    Callable in two contexts:

    - **Inline** from ``/upload/complete`` (default for normal-sized
      files; the request returns after Version Engine has the bytes so the user
      sees Completed immediately).
    - **Worker** (``etl_finalize_upload_job``) for very large files
      where the inline path would exceed HTTP timeouts. (Future use;
      currently we always go inline.)

    The two paths share semantics — task lifecycle, runtime state,
    error tagging — so polling/UI is identical.

    Memory profile: this path uses ``stage_blob_from_s3`` so the
    bytes never enter the Python process. For a 1 GB file the
    backend memory cost is the streaming hash buffer
    (~64 KiB) plus the tree node JSON (~hundreds of bytes).
    """
    task = repo.get_task(task_id)
    if not task:
        logger.warning(f"finalize_upload_to_version: task not found: {task_id}")
        return {"ok": False, "error": "task_not_found"}

    if task.status == ETLTaskStatus.CANCELLED:
        logger.info(f"finalize_upload_to_version: task cancelled, skip: {task_id}")
        return {"ok": True, "skipped": "cancelled"}

    s3_key = task.metadata.get("s3_key")
    mount_path = task.metadata.get("mount_path") or task.path
    if not s3_key or not mount_path:
        err = "Missing s3_key or mount_path in task metadata"
        task.mark_failed(err)
        task.metadata["error_stage"] = "finalize"
        repo.update_task(task)
        return {"ok": False, "error": err}

    state = await state_repo.get(task_id)
    if state is None:
        state = ETLRuntimeState(
            task_id=str(task_id),
            user_id=_creator_id(task),
            project_id=task.project_id,
            filename=task.filename,
            rule_id=task.rule_id,
            status=ETLTaskStatus.RUNNING,
            phase=ETLPhase.FINALIZE,
            progress=80,
        )
    else:
        state.status = ETLTaskStatus.RUNNING
        state.phase = ETLPhase.FINALIZE
        state.progress = max(state.progress, 80)
        state.touch()
    await state_repo.set(state)

    started_at = time.time()
    try:
        # Cancellation gate before staging. Skipping the stream-hash
        # entirely if the user already cancelled saves wall time on
        # large files where every second counts.
        latest = await state_repo.get(task_id)
        if (
            latest and latest.status == ETLTaskStatus.CANCELLED
        ) or task.status == ETLTaskStatus.CANCELLED:
            logger.info(
                f"finalize_upload_to_version: cancelled before stage, skip: {task_id}"
            )
            return {"ok": True, "skipped": "cancelled"}

        # Stage the raw upload as a Git-compatible blob object and return the
        # resulting Git object id for the tree update.
        ref = await stage_blob_from_s3(
            s3,
            project_id=task.project_id,
            src_key=s3_key,
        )
        logger.info(
            f"finalize_upload_to_version: staged blob {ref.hash[:12]} for "
            f"task={task_id} ({ref.size}B)"
        )

        from src.platform.project.write_lease import build_leased_worker_write_commands

        commands = build_leased_worker_write_commands()
        # ``verify_blobs=False`` because we just wrote the blob to
        # its version object key inside ``stage_blob_from_s3`` — it IS
        # there, no need to round-trip a HEAD.
        await commands.bulk_write_refs(
            project_id=task.project_id,
            file_refs={mount_path: ref},
            actor=f"upload:{task.created_by or 'unknown'}",
            message=f"Upload {task.filename}",
            verify_blobs=False,
            source_channel="upload",
        )

        processing_time = time.time() - started_at
        task.mark_completed(
            ETLTaskResult(
                output_path=s3_key,
                output_size=ref.size,
                processing_time=processing_time,
            )
        )
        # Surface the path on the task so dashboards / pollers can
        # link to the new file without round-tripping the metadata blob.
        task.path = mount_path
        repo.update_task(task)

        state.status = ETLTaskStatus.COMPLETED
        state.phase = ETLPhase.FINALIZE
        state.progress = 100
        state.error_message = None
        state.error_stage = None
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)

        logger.info(
            f"finalize_upload_to_version: task={task_id} -> version path={mount_path} "
            f"({ref.size}B in {processing_time:.2f}s)"
        )
        return {
            "ok": True,
            "stage": "finalize",
            "size": ref.size,
            "seconds": processing_time,
            "path": mount_path,
        }

    except asyncio.CancelledError:
        err = f"Finalize timed out (>{upload_config.etl_task_timeout}s)"
        task.mark_failed(err)
        task.metadata["error_stage"] = "finalize_timeout"
        repo.update_task(task)

        state.status = ETLTaskStatus.FAILED
        state.error_stage = "finalize_timeout"
        state.error_message = err
        state.progress = 0
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)
        logger.error(f"finalize_upload_to_version timeout task_id={task_id}")
        # Re-raise so the caller (route or worker) can decide how to
        # surface the timeout — the route returns 504, the worker
        # records it as a job failure for retry/triage.
        raise

    except Exception as e:
        err = f"Finalize failed: {e}"
        logger.error(
            f"finalize_upload_to_version failed task_id={task_id}: {e}", exc_info=True
        )
        task.mark_failed(err)
        task.metadata["error_stage"] = "finalize"
        repo.update_task(task)

        state.status = ETLTaskStatus.FAILED
        state.error_stage = "finalize"
        state.error_message = err
        state.progress = 0
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)
        return {"ok": False, "stage": "finalize", "error": err}


async def etl_finalize_upload_job(ctx: dict, task_id: str | int) -> dict:
    """
    ARQ wrapper around :func:`finalize_upload_to_version`.

    Currently unused by the default flow — ``/upload/complete`` runs
    finalize inline so users see Completed immediately and so devs
    don't have to remember to start the worker. Kept registered for
    when we add a "huge file async path" flag.
    """
    try:
        return await finalize_upload_to_version(
            task_id=task_id,
            repo=ctx["task_repository"],
            s3=ctx["s3_service"],
            state_repo=ctx["state_repo"],
        )
    except asyncio.CancelledError:
        # finalize_upload_to_version already recorded the timeout state.
        # Convert the re-raise into a structured failure dict so ARQ
        # records it like any other job failure rather than crashing
        # the worker loop.
        return {"ok": False, "stage": "finalize", "error": "timeout"}


async def finalize_uploads_to_version_batch(
    *,
    task_ids: list[str | int],
    repo,
    s3,
    state_repo: ETLStateRepositoryRedis,
) -> list[dict]:
    """
    Batch finalize: take N completed multipart uploads and commit them
    in **one** project-root push (= one visible version-control entry).

    Why this exists:
      Dropping a folder of 100 files via the single-file finalize
      runs 100 sequential ``ops.write_file`` calls. Each one pays the
      fixed per-commit cost (object staging + SQL publish), ~1.5–2s
      on a warm cache.
      Total: 150–200s for the folder. With this helper we pay that
      cost ONCE for the whole folder via ``ops.bulk_write``.

      As a bonus the audit log gets one "Uploaded 100 files" entry
      instead of 100 nearly-identical lines.

    Returns one result dict per input task (preserving order). The
    shape mirrors :func:`finalize_upload_to_version`'s return:
    ``{"ok": bool, "task_id": ..., "path": str | None,
       "size": int | None, "error": str | None}``.

    Behaviour notes:
      * Per-item failures are isolated. If task ``B``'s blob fails to
        download or copy, we still commit tasks ``A``, ``C``, ``D``
        — bouncing the whole batch would force the user to re-upload
        every file just because one was poisoned. ``B`` is marked
        FAILED individually.
      * All input tasks must belong to the same ``project_id`` (the
        upload protocol enforces this — one ``/upload/init`` request
        creates tasks under one project). We assert this and reject
        an entire mixed batch as a programming error.
      * The actual ``ops.bulk_write`` call is responsible for keeping
        the browser/upload action as one project-root product
        transaction. Child scope refs are derived afterwards for
        Git/AP clients; they are not extra user-visible commits.
    """
    results: list[dict] = []
    if not task_ids:
        return results

    # Phase 1: load tasks, validate, mark RUNNING. Drop anything that
    # can't proceed before we touch S3 — failed-fast keeps the bulk
    # path from holding zombie tasks in memory.
    prepared: list[dict] = []  # entries that survive into the bulk push
    project_id: str | None = None

    for tid in task_ids:
        task = repo.get_task(tid)
        if not task:
            results.append(
                {"ok": False, "task_id": str(tid), "error": "task_not_found"}
            )
            continue
        if task.status == ETLTaskStatus.CANCELLED:
            results.append(
                {"ok": True, "task_id": str(tid), "skipped": "cancelled"}
            )
            continue

        s3_key = task.metadata.get("s3_key")
        mount_path = task.metadata.get("mount_path") or task.path
        if not s3_key or not mount_path:
            err = "Missing s3_key or mount_path in task metadata"
            task.mark_failed(err)
            task.metadata["error_stage"] = "finalize"
            repo.update_task(task)
            results.append({"ok": False, "task_id": str(tid), "error": err})
            continue

        if project_id is None:
            project_id = task.project_id
        elif project_id != task.project_id:
            # Different project in the same batch is almost certainly
            # a client bug. Fail this item; the surviving siblings
            # still commit.
            err = f"Mixed project IDs in batch ({project_id} vs {task.project_id})"
            task.mark_failed(err)
            task.metadata["error_stage"] = "finalize"
            repo.update_task(task)
            results.append({"ok": False, "task_id": str(tid), "error": err})
            continue

        # Mark RUNNING/FINALIZE (mirrors single-file flow).
        state = await state_repo.get(tid)
        if state is None:
            state = ETLRuntimeState(
                task_id=str(tid),
                user_id=_creator_id(task),
                project_id=task.project_id,
                filename=task.filename,
                rule_id=task.rule_id,
                status=ETLTaskStatus.RUNNING,
                phase=ETLPhase.FINALIZE,
                progress=80,
            )
        else:
            state.status = ETLTaskStatus.RUNNING
            state.phase = ETLPhase.FINALIZE
            state.progress = max(state.progress, 80)
            state.touch()
        await state_repo.set(state)

        prepared.append({
            "task": task, "state": state, "s3_key": s3_key,
            "mount_path": mount_path,
        })

    if not prepared:
        return results

    # Phase 2: stage each raw upload from S3 as a Git blob object under its
    # version object key.
    #
    # Old flow (deleted):
    #   download_file(s3_key) -> bytes in RAM -> old Version Engine hash ->
    #   CopyObject src->dst -> bulk_write(files: dict[path, bytes])
    # New flow:
    #   stage_blob_from_s3(s3_key) -> BlobRef
    #   (stream raw bytes into Git loose-object bytes, then upload) ->
    #   bulk_write_refs(refs: dict[path, BlobRef])
    #
    # ``ops.bulk_write_refs`` only sees ``(hash, size)`` for the commit, and
    # the staged object is byte-compatible with Git.
    #
    # Each per-file stage runs in parallel under a semaphore. Each
    # stage is independent (different src_key, different version object
    # key) so there's no ordering constraint. Bounded concurrency
    # (8) keeps us from hammering Supabase Storage with hundreds of
    # parallel stream/download + upload requests on a giant folder upload.
    # Sequential previously cost ~1.3s per file; parallel collapses
    # most of that to a single round-trip cycle.
    #
    # Failures here are per-item: drop the bad one from the bulk
    # push but keep going. We return per-item outcomes and merge them
    # after ``gather`` in ``prepared`` order so duplicate target paths
    # keep deterministic last-write-wins semantics.
    refs_by_path: dict[str, BlobRef] = {}  # mount_path -> BlobRef
    survivors: list[dict] = []
    started_at = time.time()
    _STAGE_PARALLEL_LIMIT = 8

    async def _stage_one(entry: dict) -> tuple[str, dict]:
        """Stage one task's blob and return its deterministic outcome."""
        task = entry["task"]
        state = entry["state"]
        s3_key = entry["s3_key"]
        tid = task.task_id

        try:
            latest = await state_repo.get(tid)
            if (
                latest and latest.status == ETLTaskStatus.CANCELLED
            ) or task.status == ETLTaskStatus.CANCELLED:
                return (
                    "result",
                    {"ok": True, "task_id": str(tid), "skipped": "cancelled"},
                )

            ref = await stage_blob_from_s3(
                s3,
                project_id=task.project_id,
                src_key=s3_key,
            )
            entry["blob_ref"] = ref
            entry["size"] = ref.size
            return ("survivor", entry)

        except Exception as e:
            err = f"Stage failed: {e}"
            logger.error(
                f"finalize_uploads_to_version_batch: stage failed task={tid}: {e}",
                exc_info=True,
            )
            task.mark_failed(err)
            task.metadata["error_stage"] = "finalize_stage"
            repo.update_task(task)

            state.status = ETLTaskStatus.FAILED
            state.error_stage = "finalize_stage"
            state.error_message = err
            state.progress = 0
            state.updated_at = datetime.now(UTC)
            await state_repo.set_terminal(state)

            return (
                "result",
                {"ok": False, "task_id": str(tid), "error": err},
            )

    sem = asyncio.Semaphore(_STAGE_PARALLEL_LIMIT)

    async def _bounded_stage(entry: dict) -> tuple[str, dict]:
        async with sem:
            return await _stage_one(entry)

    stage_outcomes = await asyncio.gather(
        *(_bounded_stage(e) for e in prepared),
    )
    for kind, payload in stage_outcomes:
        if kind == "survivor":
            refs_by_path[payload["mount_path"]] = payload["blob_ref"]
            survivors.append(payload)
        else:
            results.append(payload)

    if not survivors:
        return results

    # Phase 3: ONE bulk push for all survivors. Even if files target
    # different scopes, ``bulk_write_refs`` groups per-scope and emits
    # one commit per group — typical case (folder upload) is
    # single-scope = single commit.
    #
    # ``verify_blobs=False`` is safe here because we just wrote each blob to
    # its version object key inside ``stage_blob_from_s3`` above — they ARE present, no
    # need to round-trip a HEAD per ref.
    from src.platform.project.write_lease import build_leased_worker_write_commands

    commands = build_leased_worker_write_commands()
    first_task = survivors[0]["task"]
    who = f"upload:{first_task.created_by or 'unknown'}"
    message = (
        f"Upload {len(survivors)} file{'s' if len(survivors) != 1 else ''}"
    )

    try:
        await commands.bulk_write_refs(
            project_id=project_id,
            file_refs=refs_by_path,
            actor=who,
            message=message,
            verify_blobs=False,
            source_channel="upload",
        )
    except Exception as e:
        # Whole-batch push failure → mark every survivor FAILED.
        # Successful Git-blob pre-stages stay in the version object
        # store as orphans; harmless (content-addressed dedupe will
        # reuse them on the next push of the same content).
        err = f"Bulk push failed: {e}"
        logger.error(
            f"finalize_uploads_to_version_batch: bulk push failed: {e}",
            exc_info=True,
        )
        for entry in survivors:
            task = entry["task"]
            state = entry["state"]
            task.mark_failed(err)
            task.metadata["error_stage"] = "finalize_push"
            repo.update_task(task)

            state.status = ETLTaskStatus.FAILED
            state.error_stage = "finalize_push"
            state.error_message = err
            state.progress = 0
            state.updated_at = datetime.now(UTC)
            await state_repo.set_terminal(state)

            results.append({"ok": False, "task_id": str(task.task_id), "error": err})
        return results

    # Phase 4: mark all survivors COMPLETED.
    #
    # Task PATCH (Supabase REST) and Redis state writes are
    # independent per task, so we fan them out under the same
    # semaphore as Phase 2. ``repo.update_task`` is sync (boto-style
    # Supabase client) so we wrap it in ``to_thread`` to actually
    # parallelize — without that, every PATCH would serialize on
    # the event loop's blocking call. For 3 files this saves
    # ~600ms; for 100 files it saves ~30s.
    elapsed = time.time() - started_at
    per_file_seconds = elapsed / len(survivors) if survivors else 0.0

    async def _mark_completed(entry: dict) -> None:
        task = entry["task"]
        state = entry["state"]
        mount_path = entry["mount_path"]
        size = entry["size"]

        task.mark_completed(
            ETLTaskResult(
                output_path=entry["s3_key"],
                output_size=size,
                processing_time=per_file_seconds,
            )
        )
        task.path = mount_path
        await asyncio.to_thread(repo.update_task, task)

        state.status = ETLTaskStatus.COMPLETED
        state.phase = ETLPhase.FINALIZE
        state.progress = 100
        state.error_message = None
        state.error_stage = None
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)

        results.append({
            "ok": True,
            "task_id": str(task.task_id),
            "path": mount_path,
            "size": size,
            "seconds": per_file_seconds,
        })

    async def _bounded_complete(entry: dict) -> None:
        async with sem:
            await _mark_completed(entry)

    await asyncio.gather(*(_bounded_complete(e) for e in survivors))

    logger.info(
        f"finalize_uploads_to_version_batch: committed {len(survivors)} files "
        f"to {project_id} in {elapsed:.2f}s ({per_file_seconds:.2f}s/file)"
    )
    return results


async def etl_postprocess_job(ctx: dict, task_id: str | int) -> dict:
    """
    Postprocess stage: load markdown artifact -> apply rule (LLM) -> upload JSON -> persist terminal state.
    """
    repo = ctx["task_repository"]
    s3 = ctx["s3_service"]
    llm = ctx["llm_service"]
    state_repo: ETLStateRepositoryRedis = ctx["state_repo"]

    task = repo.get_task(task_id)
    if not task:
        logger.warning(f"etl_postprocess_job: task not found: {task_id}")
        return {"ok": False, "error": "task_not_found"}

    # Respect DB-level cancellation as a safety net (e.g. Redis state expired)
    if task.status == ETLTaskStatus.CANCELLED:
        logger.info(f"etl_postprocess_job: task cancelled in DB, skip: {task_id}")
        return {"ok": True, "skipped": "cancelled"}

    state = await state_repo.get(task_id)
    if state and state.status == ETLTaskStatus.CANCELLED:
        logger.info(f"etl_postprocess_job: task cancelled, skip: {task_id}")
        return {"ok": True, "skipped": "cancelled"}

    # Ensure state exists
    if state is None:
        state = ETLRuntimeState(
            task_id=task_id,
            user_id=_creator_id(task),
            project_id=task.project_id,
            filename=task.filename,
            rule_id=task.rule_id,
            status=ETLTaskStatus.LLM_PROCESSING,
            phase=ETLPhase.POSTPROCESS,
            progress=60,
        )

    state.phase = ETLPhase.POSTPROCESS
    state.status = ETLTaskStatus.LLM_PROCESSING
    state.progress = max(state.progress, 60)
    state.attempt_postprocess += 1
    state.touch()
    await state_repo.set(state)

    started_at = time.time()
    try:
        md_key = state.artifact_mineru_markdown_key or task.metadata.get(
            "artifact_mineru_markdown_key"
        )
        if not md_key:
            raise RuntimeError("Missing markdown artifact key for postprocess")

        markdown_bytes = await s3.download_file(md_key)
        markdown = markdown_bytes.decode("utf-8")

        # Load rule (per-user)
        # NOTE: We manually create instances here instead of using FastAPI Depends()
        # because Worker runs outside the FastAPI request context.
        # RuleRepositorySupabase expects supabase.Client (with .table()), not our wrapper class.
        supabase_client = SupabaseClient().client
        rule_repo = RuleRepositorySupabase(
            supabase_client=supabase_client
        )
        rule = rule_repo.get_rule(str(task.rule_id))
        if not rule:
            raise RuntimeError(f"Rule not found: {task.rule_id}")

        # postprocess_mode=skip: no LLM calls, only wrap markdown pointer and metadata
        if getattr(rule, "postprocess_mode", "llm") == "skip":
            # BREAKING: default skip-mode output should expose markdown content directly for mounting,
            # and MUST NOT leak internal metadata (task_id/user_id/project_id/S3 keys).
            base_name = Path(task.filename).stem
            output_obj = {
                base_name: {
                    "filename": task.filename,
                    "content": markdown,
                }
            }
        else:
            # Strategy selection
            strategy = getattr(rule, "postprocess_strategy", None)
            if not strategy:
                if len(markdown) > etl_config.etl_postprocess_chunk_threshold_chars:
                    strategy = "chunked-summarize"
                else:
                    strategy = "direct-json"

            input_text = markdown
            if strategy == "chunked-summarize":
                chunks = _chunk_text(
                    markdown,
                    chunk_size=etl_config.etl_postprocess_chunk_size_chars,
                    max_chunks=etl_config.etl_postprocess_max_chunks,
                )
                summaries: list[str] = []
                for idx, ch in enumerate(chunks, start=1):
                    resp = await llm.call_text_model(
                        prompt=(
                            "Please summarize the following document chunk into key points (retain key field names/values/table information), output plain text.\n\n"
                            f"Chunk {idx}/{len(chunks)}:\n{ch}"
                        ),
                        system_prompt="You are a rigorous document summarization assistant.",
                        response_format="text",
                    )
                    summaries.append(resp.content)
                input_text = "\n\n".join(
                    [f"## Chunk {i + 1} Summary\n{t}" for i, t in enumerate(summaries)]
                )

            engine = RuleEngine(llm)
            transform = await engine.apply_rule(markdown_content=input_text, rule=rule)
            if not transform.success:
                raise ETLTransformationError(
                    transform.error or "Unknown error", str(task.rule_id)
                )
            output_obj = transform.output

        latest = await state_repo.get(task_id)
        if (
            latest and latest.status == ETLTaskStatus.CANCELLED
        ) or task.status == ETLTaskStatus.CANCELLED:
            logger.info(
                f"etl_postprocess_job: cancelled before upload, skip: {task_id}"
            )
            return {"ok": True, "skipped": "cancelled"}

        output_key = _output_json_key(task_id, _creator_id(task), task.project_id)
        output_json = json.dumps(output_obj, indent=2, ensure_ascii=False).encode(
            "utf-8"
        )
        if _reload_task_before_project_storage_write(repo, task_id, task) is None:
            return {"ok": True, "skipped": "task_not_live"}
        await s3.upload_file(
            key=output_key,
            content=output_json,
            content_type="application/json",
            metadata={"task_id": str(task_id), "rule_id": str(task.rule_id)},
        )

        processing_time = time.time() - started_at
        result = ETLTaskResult(
            output_path=output_key,
            output_size=len(output_json),
            processing_time=processing_time,
            mineru_task_id=state.provider_task_id,
        )

        mount_path = task.metadata.get("mount_path")
        mount_json_path = task.metadata.get("mount_json_path") or ""
        mount_key = task.metadata.get("mount_key") or Path(task.filename).name

        from src.platform.project.write_lease import build_leased_worker_write_commands

        commands = build_leased_worker_write_commands()

        if not mount_path:
            auto_name = task.metadata.get("auto_node_name") or f"{task_id}"
            auto_name = str(auto_name)[:12]
            mount_path = f"{auto_name}.json"
            await commands.write_bytes(
                task.project_id, mount_path,
                json.dumps({}, ensure_ascii=False).encode("utf-8"),
                actor=f"etl:{task_id}",
                message=f"ETL auto-create for {task.filename}",
                source_channel="upload",
            )
            task.metadata["mount_path"] = mount_path
            task.metadata["auto_node_created"] = True

        if getattr(rule, "postprocess_mode", "llm") == "skip":
            base_name = Path(task.filename).stem
            mount_value: Any = (
                output_obj.get(base_name, output_obj)
                if isinstance(output_obj, dict)
                else output_obj
            )
        else:
            mount_value = output_obj

        try:
            entry = commands.ops.stat(task.project_id, mount_path)

            is_pending = entry and entry.type == "file"
            if is_pending:
                if isinstance(mount_value, dict) and "content" in mount_value:
                    markdown_content = mount_value["content"]
                elif isinstance(mount_value, str):
                    markdown_content = mount_value
                else:
                    markdown_content = markdown

                md_path = mount_path
                if not md_path.endswith(".md"):
                    md_path = md_path.rsplit(".", 1)[0] + ".md" if "." in md_path else md_path + ".md"

                await commands.write_bytes(
                    task.project_id, md_path,
                    markdown_content.encode("utf-8"),
                    actor=f"etl:{task_id}",
                    message=f"OCR result for {task.filename}",
                    source_channel="upload",
                )
                logger.info(f"ETL: Filled preview for pending node {mount_path}")
            else:
                try:
                    existing_bytes = commands.ops.read_file(task.project_id, mount_path)
                    existing_content = json.loads(existing_bytes.decode("utf-8"))
                except Exception:
                    existing_content = {}

                if mount_json_path:
                    path_parts = [p for p in mount_json_path.split("/") if p]
                    current = existing_content
                    for part in path_parts[:-1]:
                        if part not in current:
                            current[part] = {}
                        current = current[part]
                    if path_parts:
                        current[path_parts[-1]] = {mount_key: mount_value}
                    else:
                        existing_content[mount_key] = mount_value
                else:
                    existing_content[mount_key] = mount_value

                await commands.write_bytes(
                    task.project_id, mount_path,
                    json.dumps(existing_content, ensure_ascii=False, indent=2).encode("utf-8"),
                    actor=f"etl:{task_id}",
                    message=f"ETL mount for {task.filename}",
                    source_channel="upload",
                )
        except Exception as e:
            # Mount failure => task failed (output exists in S3, but contract is "completed means mounted")
            err = f"Mount failed: {e}"
            task.status = ETLTaskStatus.FAILED
            task.error = err
            task.metadata["error_stage"] = "mount"
            task.metadata["output_path"] = output_key
            repo.update_task(task)

            state.status = ETLTaskStatus.FAILED
            state.error_stage = "mount"
            state.error_message = err
            state.progress = 0
            state.updated_at = datetime.now(UTC)
            await state_repo.set_terminal(state)
            return {"ok": False, "stage": "mount", "error": err}

        # Persist terminal state to DB (mounted successfully)
        task.mark_completed(result)
        repo.update_task(task)

        state.status = ETLTaskStatus.COMPLETED
        state.phase = ETLPhase.FINALIZE
        state.progress = 100
        state.error_message = None
        state.error_stage = None
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)

        return {"ok": True, "stage": "postprocess", "output_key": output_key}

    except asyncio.CancelledError:
        # Same reason as OCR job: avoid leaving Redis runtime state stuck in LLM_PROCESSING.
        md_key = state.artifact_mineru_markdown_key or task.metadata.get(
            "artifact_mineru_markdown_key"
        )
        if md_key:
            task.metadata["artifact_mineru_markdown_key"] = md_key
        if state.provider_task_id:
            task.metadata["provider_task_id"] = state.provider_task_id
        task.metadata["error_stage"] = "timeout"

        err = f"ETL postprocess job timed out (>{upload_config.etl_task_timeout}s)"
        task.status = ETLTaskStatus.FAILED
        task.error = err
        repo.update_task(task)

        state.status = ETLTaskStatus.FAILED
        state.error_stage = "timeout"
        state.error_message = err
        state.progress = 0
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)

        logger.error(f"etl_postprocess_job timeout task_id={task_id}")
        return {"ok": False, "stage": "postprocess", "error": err}

    except Exception as e:
        # Persist minimal retry pointer if OCR succeeded
        md_key = state.artifact_mineru_markdown_key or task.metadata.get(
            "artifact_mineru_markdown_key"
        )
        if md_key:
            task.metadata["artifact_mineru_markdown_key"] = md_key
        if state.provider_task_id:
            task.metadata["provider_task_id"] = state.provider_task_id
        task.metadata["error_stage"] = "postprocess"

        task.status = ETLTaskStatus.FAILED
        task.error = str(e)
        repo.update_task(task)

        state.status = ETLTaskStatus.FAILED
        state.error_stage = "postprocess"
        state.error_message = str(e)
        state.progress = 0
        state.updated_at = datetime.now(UTC)
        await state_repo.set_terminal(state)

        logger.error(
            f"etl_postprocess_job failed task_id={task_id}: {e}", exc_info=True
        )
        return {"ok": False, "stage": "postprocess", "error": str(e)}
